# Copyright (c) 2021-2022, NVIDIA CORPORATION.
"""Base class for Frame types that have an index."""

from __future__ import annotations

import numbers
import operator
import warnings
from collections import Counter, abc
from functools import cached_property
from typing import Any, Callable, Dict, Optional, Tuple, Type, TypeVar, Union
from uuid import uuid4

import cupy as cp
import numpy as np
import pandas as pd
from nvtx import annotate

import cudf
import cudf._lib as libcudf
from cudf._typing import ColumnLike
from cudf.api.types import (
    _is_non_decimal_numeric_dtype,
    is_bool_dtype,
    is_categorical_dtype,
    is_integer_dtype,
    is_list_dtype,
    is_list_like,
)
from cudf.core.column import ColumnBase
from cudf.core.column_accessor import ColumnAccessor
from cudf.core.frame import Frame, _drop_rows_by_labels
from cudf.core.index import Index, RangeIndex, _index_from_columns
from cudf.core.multiindex import MultiIndex
from cudf.core.udf.utils import _compile_or_get, _supported_cols_from_frame

doc_reset_index_template = """
        Reset the index of the {klass}, or a level of it.

        Parameters
        ----------
        level : int, str, tuple, or list, default None
            Only remove the given levels from the index. Removes all levels by
            default.
        drop : bool, default False
            Do not try to insert index into dataframe columns. This resets
            the index to the default integer index.
{argument}
        inplace : bool, default False
            Modify the DataFrame in place (do not create a new object).

        Returns
        -------
        {return_type}
            {klass} with the new index or None if ``inplace=True``.{return_doc}

        Examples
        --------
        {example}
"""


def _get_host_unique(array):
    if isinstance(array, (cudf.Series, cudf.Index, ColumnBase)):
        return array.unique.to_pandas()
    elif isinstance(array, (str, numbers.Number)):
        return [array]
    else:
        return set(array)


def _drop_columns(f: Frame, columns: abc.Iterable, errors: str):
    for c in columns:
        try:
            f._drop_column(c)
        except KeyError as e:
            if errors == "ignore":
                pass
            else:
                raise e


def _indices_from_labels(obj, labels):

    if not isinstance(labels, cudf.MultiIndex):
        labels = cudf.core.column.as_column(labels)

        if is_categorical_dtype(obj.index):
            labels = labels.astype("category")
            codes = labels.codes.astype(obj.index._values.codes.dtype)
            labels = cudf.core.column.build_categorical_column(
                categories=labels.dtype.categories,
                codes=codes,
                ordered=labels.dtype.ordered,
            )
        else:
            labels = labels.astype(obj.index.dtype)

    # join is not guaranteed to maintain the index ordering
    # so we will sort it with its initial ordering which is stored
    # in column "__"
    lhs = cudf.DataFrame(
        {"__": cudf.core.column.arange(len(labels))}, index=labels
    )
    rhs = cudf.DataFrame(
        {"_": cudf.core.column.arange(len(obj))}, index=obj.index
    )
    return lhs.join(rhs).sort_values("__")["_"]


def _get_label_range_or_mask(index, start, stop, step):
    if (
        not (start is None and stop is None)
        and type(index) is cudf.core.index.DatetimeIndex
        and index.is_monotonic is False
    ):
        start = pd.to_datetime(start)
        stop = pd.to_datetime(stop)
        if start is not None and stop is not None:
            if start > stop:
                return slice(0, 0, None)
            # TODO: Once Index binary ops are updated to support logical_and,
            # can use that instead of using cupy.
            boolean_mask = cp.logical_and((index >= start), (index <= stop))
        elif start is not None:
            boolean_mask = index >= start
        else:
            boolean_mask = index <= stop
        return boolean_mask
    else:
        start, stop = index.find_label_range(start, stop)
        return slice(start, stop, step)


class _FrameIndexer:
    """Parent class for indexers."""

    def __init__(self, frame):
        self._frame = frame


_LocIndexerClass = TypeVar("_LocIndexerClass", bound="_FrameIndexer")
_IlocIndexerClass = TypeVar("_IlocIndexerClass", bound="_FrameIndexer")

T = TypeVar("T", bound="IndexedFrame")


class IndexedFrame(Frame):
    """A frame containing an index.

    This class encodes the common behaviors for core user-facing classes like
    DataFrame and Series that consist of a sequence of columns along with a
    special set of index columns.

    Parameters
    ----------
    data : dict
        An dict mapping column names to Columns
    index : Table
        A Frame representing the (optional) index columns.
    """

    # mypy can't handle bound type variables as class members
    _loc_indexer_type: Type[_LocIndexerClass]  # type: ignore
    _iloc_indexer_type: Type[_IlocIndexerClass]  # type: ignore
    _index: cudf.core.index.BaseIndex

    def __init__(self, data=None, index=None):
        super().__init__(data=data, index=index)

    def to_dict(self, *args, **kwargs):  # noqa: D102
        raise TypeError(
            "cuDF does not support conversion to host memory "
            "via `to_dict()` method. Consider using "
            "`.to_pandas().to_dict()` to construct a Python dictionary."
        )

    @property
    def index(self):
        """Get the labels for the rows."""
        return self._index

    @index.setter
    def index(self, value):
        old_length = len(self)
        new_length = len(value)

        # A DataFrame with 0 columns can have an index of arbitrary length.
        if len(self._data) > 0 and new_length != old_length:
            raise ValueError(
                f"Length mismatch: Expected axis has {old_length} elements, "
                f"new values have {len(value)} elements"
            )
        self._index = Index(value)

    @cached_property
    def loc(self):
        """Select rows and columns by label or boolean mask.

        Examples
        --------
        **Series**

        >>> import cudf
        >>> series = cudf.Series([10, 11, 12], index=['a', 'b', 'c'])
        >>> series
        a    10
        b    11
        c    12
        dtype: int64
        >>> series.loc['b']
        11

        **DataFrame**

        DataFrame with string index.

        >>> df
           a  b
        a  0  5
        b  1  6
        c  2  7
        d  3  8
        e  4  9

        Select a single row by label.

        >>> df.loc['a']
        a    0
        b    5
        Name: a, dtype: int64

        Select multiple rows and a single column.

        >>> df.loc[['a', 'c', 'e'], 'b']
        a    5
        c    7
        e    9
        Name: b, dtype: int64

        Selection by boolean mask.

        >>> df.loc[df.a > 2]
           a  b
        d  3  8
        e  4  9

        Setting values using loc.

        >>> df.loc[['a', 'c', 'e'], 'a'] = 0
        >>> df
           a  b
        a  0  5
        b  1  6
        c  0  7
        d  3  8
        e  0  9

        """
        return self._loc_indexer_type(self)

    @cached_property
    def iloc(self):
        """Select values by position.

        Examples
        --------
        **Series**

        >>> import cudf
        >>> s = cudf.Series([10, 20, 30])
        >>> s
        0    10
        1    20
        2    30
        dtype: int64
        >>> s.iloc[2]
        30

        **DataFrame**

        Selecting rows and column by position.

        >>> df = cudf.DataFrame({'a': range(20),
        ...                      'b': range(20),
        ...                      'c': range(20)})

        Select a single row using an integer index.

        >>> df.iloc[1]
        a    1
        b    1
        c    1
        Name: 1, dtype: int64

        Select multiple rows using a list of integers.

        >>> df.iloc[[0, 2, 9, 18]]
              a    b    c
         0    0    0    0
         2    2    2    2
         9    9    9    9
        18   18   18   18

        Select rows using a slice.

        >>> df.iloc[3:10:2]
             a    b    c
        3    3    3    3
        5    5    5    5
        7    7    7    7
        9    9    9    9

        Select both rows and columns.

        >>> df.iloc[[1, 3, 5, 7], 2]
        1    1
        3    3
        5    5
        7    7
        Name: c, dtype: int64

        Setting values in a column using iloc.

        >>> df.iloc[:4] = 0
        >>> df
           a  b  c
        0  0  0  0
        1  0  0  0
        2  0  0  0
        3  0  0  0
        4  4  4  4
        5  5  5  5
        6  6  6  6
        7  7  7  7
        8  8  8  8
        9  9  9  9
        [10 more rows]

        """
        return self._iloc_indexer_type(self)

    @annotate("SORT_INDEX", color="red", domain="cudf_python")
    def sort_index(
        self,
        axis=0,
        level=None,
        ascending=True,
        inplace=False,
        kind=None,
        na_position="last",
        sort_remaining=True,
        ignore_index=False,
        key=None,
    ):
        """Sort object by labels (along an axis).

        Parameters
        ----------
        axis : {0 or 'index', 1 or 'columns'}, default 0
            The axis along which to sort. The value 0 identifies the rows,
            and 1 identifies the columns.
        level : int or level name or list of ints or list of level names
            If not None, sort on values in specified index level(s).
            This is only useful in the case of MultiIndex.
        ascending : bool, default True
            Sort ascending vs. descending.
        inplace : bool, default False
            If True, perform operation in-place.
        kind : sorting method such as `quick sort` and others.
            Not yet supported.
        na_position : {'first', 'last'}, default 'last'
            Puts NaNs at the beginning if first; last puts NaNs at the end.
        sort_remaining : bool, default True
            Not yet supported
        ignore_index : bool, default False
            if True, index will be replaced with RangeIndex.
        key : callable, optional
            If not None, apply the key function to the index values before
            sorting. This is similar to the key argument in the builtin
            sorted() function, with the notable difference that this key
            function should be vectorized. It should expect an Index and return
            an Index of the same shape. For MultiIndex inputs, the key is
            applied per level.

        Returns
        -------
        Frame or None

        Notes
        -----
        Difference from pandas:
          * Not supporting: kind, sort_remaining=False

        Examples
        --------
        **Series**
        >>> import cudf
        >>> series = cudf.Series(['a', 'b', 'c', 'd'], index=[3, 2, 1, 4])
        >>> series
        3    a
        2    b
        1    c
        4    d
        dtype: object
        >>> series.sort_index()
        1    c
        2    b
        3    a
        4    d
        dtype: object

        Sort Descending

        >>> series.sort_index(ascending=False)
        4    d
        3    a
        2    b
        1    c
        dtype: object

        **DataFrame**
        >>> df = cudf.DataFrame(
        ... {"b":[3, 2, 1], "a":[2, 1, 3]}, index=[1, 3, 2])
        >>> df.sort_index(axis=0)
           b  a
        1  3  2
        2  1  3
        3  2  1
        >>> df.sort_index(axis=1)
           a  b
        1  2  3
        3  1  2
        2  3  1
        """
        if kind is not None:
            raise NotImplementedError("kind is not yet supported")

        if not sort_remaining:
            raise NotImplementedError(
                "sort_remaining == False is not yet supported"
            )

        if key is not None:
            raise NotImplementedError("key is not yet supported.")

        if na_position not in {"first", "last"}:
            raise ValueError(f"invalid na_position: {na_position}")

        if axis in (0, "index"):
            idx = self.index
            if isinstance(idx, MultiIndex):
                if level is not None:
                    # Pandas doesn't handle na_position in case of MultiIndex.
                    na_position = "first" if ascending is True else "last"
                    labels = [
                        idx._get_level_label(lvl)
                        for lvl in (level if is_list_like(level) else (level,))
                    ]
                    # Explicitly construct a Frame rather than using type(self)
                    # to avoid constructing a SingleColumnFrame (e.g. Series).
                    idx = Frame._from_data(idx._data.select_by_label(labels))

                inds = idx._get_sorted_inds(
                    ascending=ascending, na_position=na_position
                )
                out = self._gather(inds)
                # TODO: frame factory function should handle multilevel column
                # names
                if (
                    isinstance(self, cudf.core.dataframe.DataFrame)
                    and self._data.multiindex
                ):
                    out._set_column_names_like(self)
            elif (ascending and idx.is_monotonic_increasing) or (
                not ascending and idx.is_monotonic_decreasing
            ):
                out = self.copy()
            else:
                inds = idx.argsort(
                    ascending=ascending, na_position=na_position
                )
                out = self._gather(inds)
                if (
                    isinstance(self, cudf.core.dataframe.DataFrame)
                    and self._data.multiindex
                ):
                    out._set_column_names_like(self)
        else:
            labels = sorted(self._data.names, reverse=not ascending)
            out = self[labels]

        if ignore_index is True:
            out = out.reset_index(drop=True)
        return self._mimic_inplace(out, inplace=inplace)

    def memory_usage(self, index=True, deep=False):
        """Return the memory usage of an object.

        Parameters
        ----------
        index : bool, default True
            Specifies whether to include the memory usage of the index.
        deep : bool, default False
            The deep parameter is ignored and is only included for pandas
            compatibility.

        Returns
        -------
        Series or scalar
            For DataFrame, a Series whose index is the original column names
            and whose values is the memory usage of each column in bytes. For a
            Series the total memory usage.

        Examples
        --------
        **DataFrame**

        >>> dtypes = ['int64', 'float64', 'object', 'bool']
        >>> data = dict([(t, np.ones(shape=5000).astype(t))
        ...              for t in dtypes])
        >>> df = cudf.DataFrame(data)
        >>> df.head()
           int64  float64  object  bool
        0      1      1.0     1.0  True
        1      1      1.0     1.0  True
        2      1      1.0     1.0  True
        3      1      1.0     1.0  True
        4      1      1.0     1.0  True
        >>> df.memory_usage(index=False)
        int64      40000
        float64    40000
        object     40000
        bool        5000
        dtype: int64

        Use a Categorical for efficient storage of an object-dtype column with
        many repeated values.

        >>> df['object'].astype('category').memory_usage(deep=True)
        5008

        **Series**
        >>> s = cudf.Series(range(3), index=['a','b','c'])
        >>> s.memory_usage()
        43

        Not including the index gives the size of the rest of the data, which
        is necessarily smaller:

        >>> s.memory_usage(index=False)
        24
        """
        usage = super().memory_usage(deep=deep)
        if index:
            usage["Index"] = self.index.memory_usage()
        return usage

    def hash_values(self, method="murmur3"):
        """Compute the hash of values in this column.

        Parameters
        ----------
        method : {'murmur3', 'md5'}, default 'murmur3'
            Hash function to use:
            * murmur3: MurmurHash3 hash function.
            * md5: MD5 hash function.

        Returns
        -------
        Series
            A Series with hash values.

        Examples
        --------
        **Series**

        >>> import cudf
        >>> series = cudf.Series([10, 120, 30])
        >>> series
        0     10
        1    120
        2     30
        dtype: int64
        >>> series.hash_values(method="murmur3")
        0   -1930516747
        1     422619251
        2    -941520876
        dtype: int32
        >>> series.hash_values(method="md5")
        0    7be4bbacbfdb05fb3044e36c22b41e8b
        1    947ca8d2c5f0f27437f156cfbfab0969
        2    d0580ef52d27c043c8e341fd5039b166
        dtype: object

        **DataFrame**

        >>> import cudf
        >>> df = cudf.DataFrame({"a": [10, 120, 30], "b": [0.0, 0.25, 0.50]})
        >>> df
             a     b
        0   10  0.00
        1  120  0.25
        2   30  0.50
        >>> df.hash_values(method="murmur3")
        0    -330519225
        1    -397962448
        2   -1345834934
        dtype: int32
        >>> df.hash_values(method="md5")
        0    57ce879751b5169c525907d5c563fae1
        1    948d6221a7c4963d4be411bcead7e32b
        2    fe061786ea286a515b772d91b0dfcd70
        dtype: object
        """
        # Note that both Series and DataFrame return Series objects from this
        # calculation, necessitating the unfortunate circular reference to the
        # child class here.
        return cudf.Series._from_data(
            {None: libcudf.hash.hash(self, method)}, index=self.index
        )

    def _gather(
        self, gather_map, keep_index=True, nullify=False, check_bounds=True
    ):
        """Gather rows of frame specified by indices in `gather_map`.

        Skip bounds checking if check_bounds is False.
        Set rows to null for all out of bound indices if nullify is `True`.
        """
        gather_map = cudf.core.column.as_column(gather_map)

        # TODO: For performance, the check and conversion of gather map should
        # be done by the caller. This check will be removed in future release.
        if not is_integer_dtype(gather_map.dtype):
            gather_map = gather_map.astype("int32")

        if not libcudf.copying._gather_map_is_valid(
            gather_map, len(self), check_bounds, nullify
        ):
            raise IndexError("Gather map index is out of bounds.")

        return self._from_columns_like_self(
            libcudf.copying.gather(
                list(self._index._columns + self._columns)
                if keep_index
                else list(self._columns),
                gather_map,
                nullify=nullify,
            ),
            self._column_names,
            self._index.names if keep_index else None,
        )

    def _positions_from_column_names(
        self, column_names, offset_by_index_columns=False
    ):
        """Map each column name into their positions in the frame.

        Return positions of the provided column names, offset by the number of
        index columns if `offset_by_index_columns` is True. The order of
        indices returned corresponds to the column order in this Frame.
        """
        num_index_columns = (
            len(self._index._data) if offset_by_index_columns else 0
        )
        return [
            i + num_index_columns
            for i, name in enumerate(self._column_names)
            if name in set(column_names)
        ]

    def drop_duplicates(
        self,
        subset=None,
        keep="first",
        nulls_are_equal=True,
        ignore_index=False,
    ):
        """
        Drop duplicate rows in frame.

        subset : list, optional
            List of columns to consider when dropping rows.
        keep : ["first", "last", False]
            "first" will keep the first duplicate entry, "last" will keep the
            last duplicate entry, and False will drop all duplicates.
        nulls_are_equal: bool, default True
            Null elements are considered equal to other null elements.
        ignore_index: bool, default False
            If True, the resulting axis will be labeled 0, 1, ..., n - 1.
        """
        if subset is None:
            subset = self._column_names
        elif (
            not np.iterable(subset)
            or isinstance(subset, str)
            or isinstance(subset, tuple)
            and subset in self._data.names
        ):
            subset = (subset,)
        diff = set(subset) - set(self._data)
        if len(diff) != 0:
            raise KeyError(f"columns {diff} do not exist")
        subset_cols = [name for name in self._column_names if name in subset]
        if len(subset_cols) == 0:
            return self.copy(deep=True)

        keys = self._positions_from_column_names(
            subset, offset_by_index_columns=not ignore_index
        )
        return self._from_columns_like_self(
            libcudf.stream_compaction.drop_duplicates(
                list(self._columns)
                if ignore_index
                else list(self._index._columns + self._columns),
                keys=keys,
                keep=keep,
                nulls_are_equal=nulls_are_equal,
            ),
            self._column_names,
            self._index.names if not ignore_index else None,
        )

    @annotate("FRAME_EMPTY_LIKE", color="green", domain="cudf_python")
    def _empty_like(self, keep_index=True):
        return self._from_columns_like_self(
            libcudf.copying.columns_empty_like(
                [
                    *(self._index._data.columns if keep_index else ()),
                    *self._columns,
                ]
            ),
            self._column_names,
            self._index.names if keep_index else None,
        )

    def _split(self, splits, keep_index=True):
        columns_split = libcudf.copying.columns_split(
            [
                *(self._index._data.columns if keep_index else []),
                *self._columns,
            ],
            splits,
        )

        return [
            self._from_columns_like_self(
                columns_split[i],
                self._column_names,
                self._index.names if keep_index else None,
            )
            for i in range(len(splits) + 1)
        ]

    def add_prefix(self, prefix):
        """
        Prefix labels with string `prefix`.

        For Series, the row labels are prefixed.
        For DataFrame, the column labels are prefixed.

        Parameters
        ----------
        prefix : str
            The string to add before each label.

        Returns
        -------
        Series or DataFrame
            New Series with updated labels or DataFrame with updated labels.

        See Also
        --------
        Series.add_suffix: Suffix row labels with string 'suffix'.
        DataFrame.add_suffix: Suffix column labels with string 'suffix'.

        Examples
        --------
        **Series**
        >>> s = cudf.Series([1, 2, 3, 4])
        >>> s
        0    1
        1    2
        2    3
        3    4
        dtype: int64
        >>> s.add_prefix('item_')
        item_0    1
        item_1    2
        item_2    3
        item_3    4
        dtype: int64

        **DataFrame**
        >>> df = cudf.DataFrame({'A': [1, 2, 3, 4], 'B': [3, 4, 5, 6]})
        >>> df
           A  B
        0  1  3
        1  2  4
        2  3  5
        3  4  6
        >>> df.add_prefix('col_')
             col_A  col_B
        0       1       3
        1       2       4
        2       3       5
        3       4       6
        """
        raise NotImplementedError(
            "`IndexedFrame.add_prefix` not currently implemented. \
                Use `Series.add_prefix` or `DataFrame.add_prefix`"
        )

    def add_suffix(self, suffix):
        """
        Suffix labels with string `suffix`.

        For Series, the row labels are suffixed.
        For DataFrame, the column labels are suffixed.

        Parameters
        ----------
        prefix : str
            The string to add after each label.

        Returns
        -------
        Series or DataFrame
            New Series with updated labels or DataFrame with updated labels.

        See Also
        --------
        Series.add_prefix: prefix row labels with string 'prefix'.
        DataFrame.add_prefix: Prefix column labels with string 'prefix'.

        Examples
        --------
        **Series**
        >>> s = cudf.Series([1, 2, 3, 4])
        >>> s
        0    1
        1    2
        2    3
        3    4
        dtype: int64
        >>> s.add_suffix('_item')
        0_item    1
        1_item    2
        2_item    3
        3_item    4
        dtype: int64

        **DataFrame**
        >>> df = cudf.DataFrame({'A': [1, 2, 3, 4], 'B': [3, 4, 5, 6]})
        >>> df
           A  B
        0  1  3
        1  2  4
        2  3  5
        3  4  6
        >>> df.add_suffix('_col')
             A_col  B_col
        0       1       3
        1       2       4
        2       3       5
        3       4       6
        """
        raise NotImplementedError(
            "`IndexedFrame.add_suffix` not currently implemented. \
                Use `Series.add_suffix` or `DataFrame.add_suffix`"
        )

    @annotate("APPLY", color="purple", domain="cudf_python")
    def _apply(self, func, kernel_getter, *args, **kwargs):
        """Apply `func` across the rows of the frame."""
        if kwargs:
            raise ValueError("UDFs using **kwargs are not yet supported.")

        try:
            kernel, retty = _compile_or_get(
                self, func, args, kernel_getter=kernel_getter
            )
        except Exception as e:
            raise ValueError(
                "user defined function compilation failed."
            ) from e

        # Mask and data column preallocated
        ans_col = cp.empty(len(self), dtype=retty)
        ans_mask = cudf.core.column.column_empty(len(self), dtype="bool")
        launch_args = [(ans_col, ans_mask), len(self)]
        offsets = []

        # if _compile_or_get succeeds, it is safe to create a kernel that only
        # consumes the columns that are of supported dtype
        for col in _supported_cols_from_frame(self).values():
            data = col.data
            mask = col.mask
            if mask is None:
                launch_args.append(data)
            else:
                launch_args.append((data, mask))
            offsets.append(col.offset)
        launch_args += offsets
        launch_args += list(args)

        try:
            kernel.forall(len(self))(*launch_args)
        except Exception as e:
            raise RuntimeError("UDF kernel execution failed.") from e

        col = cudf.core.column.as_column(ans_col)
        col.set_base_mask(libcudf.transform.bools_to_mask(ans_mask))
        result = cudf.Series._from_data({None: col}, self._index)

        return result

    def sort_values(
        self,
        by,
        axis=0,
        ascending=True,
        inplace=False,
        kind="quicksort",
        na_position="last",
        ignore_index=False,
    ):
        """Sort by the values along either axis.

        Parameters
        ----------
        by : str or list of str
            Name or list of names to sort by.
        ascending : bool or list of bool, default True
            Sort ascending vs. descending. Specify list for multiple sort
            orders. If this is a list of bools, must match the length of the
            by.
        na_position : {‘first’, ‘last’}, default ‘last’
            'first' puts nulls at the beginning, 'last' puts nulls at the end
        ignore_index : bool, default False
            If True, index will not be sorted.

        Returns
        -------
        Frame : Frame with sorted values.

        Notes
        -----
        Difference from pandas:
          * Support axis='index' only.
          * Not supporting: inplace, kind

        Examples
        --------
        >>> import cudf
        >>> df = cudf.DataFrame()
        >>> df['a'] = [0, 1, 2]
        >>> df['b'] = [-3, 2, 0]
        >>> df.sort_values('b')
           a  b
        0  0 -3
        2  2  0
        1  1  2
        """
        if na_position not in {"first", "last"}:
            raise ValueError(f"invalid na_position: {na_position}")
        if inplace:
            raise NotImplementedError("`inplace` not currently implemented.")
        if kind != "quicksort":
            if kind not in {"mergesort", "heapsort", "stable"}:
                raise AttributeError(
                    f"{kind} is not a valid sorting algorithm for "
                    f"'DataFrame' object"
                )
            warnings.warn(
                f"GPU-accelerated {kind} is currently not supported, "
                f"defaulting to quicksort."
            )
        if axis != 0:
            raise NotImplementedError("`axis` not currently implemented.")

        if len(self) == 0:
            return self

        # argsort the `by` column
        out = self._gather(
            self._get_columns_by_label(by)._get_sorted_inds(
                ascending=ascending, na_position=na_position
            ),
            keep_index=not ignore_index,
        )
        if (
            isinstance(self, cudf.core.dataframe.DataFrame)
            and self._data.multiindex
        ):
            out.columns = self._data.to_pandas_index()
        return out

    def _n_largest_or_smallest(self, largest, n, columns, keep):
        # Get column to operate on
        if isinstance(columns, str):
            columns = [columns]

        if len(self) == 0:
            return self

        if keep == "first":
            if n < 0:
                n = 0

            # argsort the `by` column
            return self._gather(
                self._get_columns_by_label(columns)._get_sorted_inds(
                    ascending=not largest
                )[:n],
                keep_index=True,
                check_bounds=False,
            )
        elif keep == "last":
            indices = self._get_columns_by_label(columns)._get_sorted_inds(
                ascending=largest
            )

            if n <= 0:
                # Empty slice.
                indices = indices[0:0]
            else:
                indices = indices[: -n - 1 : -1]
            return self._gather(indices, keep_index=True, check_bounds=False)
        else:
            raise ValueError('keep must be either "first", "last"')

    def _align_to_index(
        self: T,
        index: ColumnLike,
        how: str = "outer",
        sort: bool = True,
        allow_non_unique: bool = False,
    ) -> T:
        index = cudf.core.index.as_index(index)

        if self.index.equals(index):
            return self
        if not allow_non_unique:
            if not self.index.is_unique or not index.is_unique:
                raise ValueError("Cannot align indices with non-unique values")

        lhs = cudf.DataFrame._from_data(self._data, index=self.index)
        rhs = cudf.DataFrame._from_data({}, index=index)

        # create a temporary column that we will later sort by
        # to recover ordering after index alignment.
        sort_col_id = str(uuid4())
        if how == "left":
            lhs[sort_col_id] = cudf.core.column.arange(len(lhs))
        elif how == "right":
            rhs[sort_col_id] = cudf.core.column.arange(len(rhs))

        result = lhs.join(rhs, how=how, sort=sort)
        if how in ("left", "right"):
            result = result.sort_values(sort_col_id)
            del result[sort_col_id]

        result = self.__class__._from_data(result._data, index=result.index)
        result._data.multiindex = self._data.multiindex
        result._data._level_names = self._data._level_names
        result.index.names = self.index.names

        return result

    def round(self, decimals=0, how="half_even"):
        """
        Round to a variable number of decimal places.

        Parameters
        ----------
        decimals : int, dict, Series
            Number of decimal places to round each column to. This parameter
            must be an int for a Series. For a DataFrame, a dict or a Series
            are also valid inputs. If an int is given, round each column to the
            same number of places. Otherwise dict and Series round to variable
            numbers of places. Column names should be in the keys if
            `decimals` is a dict-like, or in the index if `decimals` is a
            Series. Any columns not included in `decimals` will be left as is.
            Elements of `decimals` which are not columns of the input will be
            ignored.
        how : str, optional
            Type of rounding. Can be either "half_even" (default)
            or "half_up" rounding.

        Returns
        -------
        Series or DataFrame
            A Series or DataFrame with the affected columns rounded to the
            specified number of decimal places.

        Examples
        --------
        **Series**

        >>> s = cudf.Series([0.1, 1.4, 2.9])
        >>> s.round()
        0    0.0
        1    1.0
        2    3.0
        dtype: float64

        **DataFrame**

        >>> df = cudf.DataFrame(
        ...     [(.21, .32), (.01, .67), (.66, .03), (.21, .18)],
        ...     columns=['dogs', 'cats'],
        ... )
        >>> df
           dogs  cats
        0  0.21  0.32
        1  0.01  0.67
        2  0.66  0.03
        3  0.21  0.18

        By providing an integer each column is rounded to the same number
        of decimal places.

        >>> df.round(1)
           dogs  cats
        0   0.2   0.3
        1   0.0   0.7
        2   0.7   0.0
        3   0.2   0.2

        With a dict, the number of places for specific columns can be
        specified with the column names as keys and the number of decimal
        places as values.

        >>> df.round({'dogs': 1, 'cats': 0})
           dogs  cats
        0   0.2   0.0
        1   0.0   1.0
        2   0.7   0.0
        3   0.2   0.0

        Using a Series, the number of places for specific columns can be
        specified with the column names as the index and the number of
        decimal places as the values.

        >>> decimals = cudf.Series([0, 1], index=['cats', 'dogs'])
        >>> df.round(decimals)
           dogs  cats
        0   0.2   0.0
        1   0.0   1.0
        2   0.7   0.0
        3   0.2   0.0
        """
        if isinstance(decimals, cudf.Series):
            decimals = decimals.to_pandas()

        if isinstance(decimals, pd.Series):
            if not decimals.index.is_unique:
                raise ValueError("Index of decimals must be unique")
            decimals = decimals.to_dict()
        elif isinstance(decimals, int):
            decimals = {name: decimals for name in self._column_names}
        elif not isinstance(decimals, abc.Mapping):
            raise TypeError(
                "decimals must be an integer, a dict-like or a Series"
            )

        cols = {
            name: col.round(decimals[name], how=how)
            if (name in decimals and _is_non_decimal_numeric_dtype(col.dtype))
            else col.copy(deep=True)
            for name, col in self._data.items()
        }

        return self.__class__._from_data(
            data=cudf.core.column_accessor.ColumnAccessor(
                cols,
                multiindex=self._data.multiindex,
                level_names=self._data.level_names,
            ),
            index=self._index,
        )

    def resample(
        self,
        rule,
        axis=0,
        closed=None,
        label=None,
        convention="start",
        kind=None,
        loffset=None,
        base=None,
        on=None,
        level=None,
        origin="start_day",
        offset=None,
    ):
        """
        Convert the frequency of ("resample") the given time series data.

        Parameters
        ----------
        rule: str
            The offset string representing the frequency to use.
            Note that DateOffset objects are not yet supported.
        closed: {"right", "left"}, default None
            Which side of bin interval is closed. The default is
            "left" for all frequency offsets except for "M" and "W",
            which have a default of "right".
        label: {"right", "left"}, default None
            Which bin edge label to label bucket with. The default is
            "left" for all frequency offsets except for "M" and "W",
            which have a default of "right".
        on: str, optional
            For a DataFrame, column to use instead of the index for
            resampling.  Column must be a datetime-like.
        level: str or int, optional
            For a MultiIndex, level to use instead of the index for
            resampling.  The level must be a datetime-like.

        Returns
        -------
        A Resampler object

        Examples
        --------
        First, we create a time series with 1 minute intervals:

        >>> index = cudf.date_range(start="2001-01-01", periods=10, freq="1T")
        >>> sr = cudf.Series(range(10), index=index)
        >>> sr
        2001-01-01 00:00:00    0
        2001-01-01 00:01:00    1
        2001-01-01 00:02:00    2
        2001-01-01 00:03:00    3
        2001-01-01 00:04:00    4
        2001-01-01 00:05:00    5
        2001-01-01 00:06:00    6
        2001-01-01 00:07:00    7
        2001-01-01 00:08:00    8
        2001-01-01 00:09:00    9
        dtype: int64

        Downsampling to 3 minute intervals, followed by a "sum" aggregation:

        >>> sr.resample("3T").sum()
        2001-01-01 00:00:00     3
        2001-01-01 00:03:00    12
        2001-01-01 00:06:00    21
        2001-01-01 00:09:00     9
        dtype: int64

        Use the right side of each interval to label the bins:

        >>> sr.resample("3T", label="right").sum()
        2001-01-01 00:03:00     3
        2001-01-01 00:06:00    12
        2001-01-01 00:09:00    21
        2001-01-01 00:12:00     9
        dtype: int64

        Close the right side of the interval instead of the left:

        >>> sr.resample("3T", closed="right").sum()
        2000-12-31 23:57:00     0
        2001-01-01 00:00:00     6
        2001-01-01 00:03:00    15
        2001-01-01 00:06:00    24
        dtype: int64

        Upsampling to 30 second intervals:

        >>> sr.resample("30s").asfreq()[:5]  # show the first 5 rows
        2001-01-01 00:00:00       0
        2001-01-01 00:00:30    <NA>
        2001-01-01 00:01:00       1
        2001-01-01 00:01:30    <NA>
        2001-01-01 00:02:00       2
        dtype: int64

        Upsample and fill nulls using the "bfill" method:

        >>> sr.resample("30s").bfill()[:5]
        2001-01-01 00:00:00    0
        2001-01-01 00:00:30    1
        2001-01-01 00:01:00    1
        2001-01-01 00:01:30    2
        2001-01-01 00:02:00    2
        dtype: int64

        Resampling by a specified column of a Dataframe:

        >>> df = cudf.DataFrame({
        ...     "price": [10, 11, 9, 13, 14, 18, 17, 19],
        ...     "volume": [50, 60, 40, 100, 50, 100, 40, 50],
        ...     "week_starting": cudf.date_range(
        ...         "2018-01-01", periods=8, freq="7D"
        ...     )
        ... })
        >>> df
        price  volume week_starting
        0     10      50    2018-01-01
        1     11      60    2018-01-08
        2      9      40    2018-01-15
        3     13     100    2018-01-22
        4     14      50    2018-01-29
        5     18     100    2018-02-05
        6     17      40    2018-02-12
        7     19      50    2018-02-19
        >>> df.resample("M", on="week_starting").mean()
                       price     volume
        week_starting
        2018-01-31      11.4  60.000000
        2018-02-28      18.0  63.333333


        Notes
        -----
        Note that the dtype of the index (or the 'on' column if using
        'on=') in the result will be of a frequency closest to the
        resampled frequency.  For example, if resampling from
        nanoseconds to milliseconds, the index will be of dtype
        'datetime64[ms]'.
        """
        import cudf.core.resample

        if (axis, convention, kind, loffset, base, origin, offset) != (
            0,
            "start",
            None,
            None,
            None,
            "start_day",
            None,
        ):
            raise NotImplementedError(
                "The following arguments are not "
                "currently supported by resample:\n\n"
                "- axis\n"
                "- convention\n"
                "- kind\n"
                "- loffset\n"
                "- base\n"
                "- origin\n"
                "- offset"
            )
        by = cudf.Grouper(
            key=on, freq=rule, closed=closed, label=label, level=level
        )
        return (
            cudf.core.resample.SeriesResampler(self, by=by)
            if isinstance(self, cudf.Series)
            else cudf.core.resample.DataFrameResampler(self, by=by)
        )

    def dropna(
        self, axis=0, how="any", thresh=None, subset=None, inplace=False
    ):
        """
        Drop rows (or columns) containing nulls from a Column.

        Parameters
        ----------
        axis : {0, 1}, optional
            Whether to drop rows (axis=0, default) or columns (axis=1)
            containing nulls.
        how : {"any", "all"}, optional
            Specifies how to decide whether to drop a row (or column).
            any (default) drops rows (or columns) containing at least
            one null value. all drops only rows (or columns) containing
            *all* null values.
        thresh: int, optional
            If specified, then drops every row (or column) containing
            less than `thresh` non-null values
        subset : list, optional
            List of columns to consider when dropping rows (all columns
            are considered by default). Alternatively, when dropping
            columns, subset is a list of rows to consider.
        inplace : bool, default False
            If True, do operation inplace and return None.

        Returns
        -------
        Copy of the DataFrame with rows/columns containing nulls dropped.

        See also
        --------
        cudf.DataFrame.isna
            Indicate null values.

        cudf.DataFrame.notna
            Indicate non-null values.

        cudf.DataFrame.fillna
            Replace null values.

        cudf.Series.dropna
            Drop null values.

        cudf.Index.dropna
            Drop null indices.

        Examples
        --------
        >>> import cudf
        >>> df = cudf.DataFrame({"name": ['Alfred', 'Batman', 'Catwoman'],
        ...                    "toy": ['Batmobile', None, 'Bullwhip'],
        ...                    "born": [np.datetime64("1940-04-25"),
        ...                             np.datetime64("NaT"),
        ...                             np.datetime64("NaT")]})
        >>> df
               name        toy                 born
        0    Alfred  Batmobile  1940-04-25 00:00:00
        1    Batman       <NA>                 <NA>
        2  Catwoman   Bullwhip                 <NA>

        Drop the rows where at least one element is null.

        >>> df.dropna()
             name        toy       born
        0  Alfred  Batmobile 1940-04-25

        Drop the columns where at least one element is null.

        >>> df.dropna(axis='columns')
               name
        0    Alfred
        1    Batman
        2  Catwoman

        Drop the rows where all elements are null.

        >>> df.dropna(how='all')
               name        toy                 born
        0    Alfred  Batmobile  1940-04-25 00:00:00
        1    Batman       <NA>                 <NA>
        2  Catwoman   Bullwhip                 <NA>

        Keep only the rows with at least 2 non-null values.

        >>> df.dropna(thresh=2)
               name        toy                 born
        0    Alfred  Batmobile  1940-04-25 00:00:00
        2  Catwoman   Bullwhip                 <NA>

        Define in which columns to look for null values.

        >>> df.dropna(subset=['name', 'born'])
             name        toy       born
        0  Alfred  Batmobile 1940-04-25

        Keep the DataFrame with valid entries in the same variable.

        >>> df.dropna(inplace=True)
        >>> df
             name        toy       born
        0  Alfred  Batmobile 1940-04-25
        """
        if axis == 0:
            result = self._drop_na_rows(how=how, subset=subset, thresh=thresh)
        else:
            result = self._drop_na_columns(
                how=how, subset=subset, thresh=thresh
            )

        return self._mimic_inplace(result, inplace=inplace)

    def _drop_na_rows(self, how="any", subset=None, thresh=None):
        """
        Drop null rows from `self`.

        how : {"any", "all"}, optional
            Specifies how to decide whether to drop a row.
            any (default) drops rows containing at least
            one null value. all drops only rows containing
            *all* null values.
        subset : list, optional
            List of columns to consider when dropping rows.
        thresh : int, optional
            If specified, then drops every row containing
            less than `thresh` non-null values.
        """
        if subset is None:
            subset = self._column_names
        elif (
            not np.iterable(subset)
            or isinstance(subset, str)
            or isinstance(subset, tuple)
            and subset in self._data.names
        ):
            subset = (subset,)
        diff = set(subset) - set(self._data)
        if len(diff) != 0:
            raise KeyError(f"columns {diff} do not exist")

        if len(subset) == 0:
            return self.copy(deep=True)

        data_columns = [
            col.nans_to_nulls()
            if isinstance(col, cudf.core.column.NumericalColumn)
            else col
            for col in self._columns
        ]

        return self._from_columns_like_self(
            libcudf.stream_compaction.drop_nulls(
                [*self._index._data.columns, *data_columns],
                how=how,
                keys=self._positions_from_column_names(
                    subset, offset_by_index_columns=True
                ),
                thresh=thresh,
            ),
            self._column_names,
            self._index.names,
        )

    def _apply_boolean_mask(self, boolean_mask):
        """Apply boolean mask to each row of `self`.

        Rows corresponding to `False` is dropped.
        """
        boolean_mask = cudf.core.column.as_column(boolean_mask)
        if not is_bool_dtype(boolean_mask.dtype):
            raise ValueError("boolean_mask is not boolean type.")

        return self._from_columns_like_self(
            libcudf.stream_compaction.apply_boolean_mask(
                list(self._index._columns + self._columns), boolean_mask
            ),
            column_names=self._column_names,
            index_names=self._index.names,
        )

    def take(self, indices, axis=0):
        """Return a new frame containing the rows specified by *indices*.

        Parameters
        ----------
        indices : array-like
            Array of ints indicating which positions to take.
        axis : Unsupported

        Returns
        -------
        out : Series or DataFrame
            New object with desired subset of rows.

        Examples
        --------
        **Series**
        >>> s = cudf.Series(['a', 'b', 'c', 'd', 'e'])
        >>> s.take([2, 0, 4, 3])
        2    c
        0    a
        4    e
        3    d
        dtype: object

        **DataFrame**

        >>> a = cudf.DataFrame({'a': [1.0, 2.0, 3.0],
        ...                    'b': cudf.Series(['a', 'b', 'c'])})
        >>> a.take([0, 2, 2])
             a  b
        0  1.0  a
        2  3.0  c
        2  3.0  c
        >>> a.take([True, False, True])
             a  b
        0  1.0  a
        2  3.0  c
        """
        if self._get_axis_from_axis_arg(axis) != 0:
            raise NotImplementedError("Only axis=0 is supported.")

        return self._gather(indices)

    def _reset_index(self, level, drop, col_level=0, col_fill=""):
        """Shared path for DataFrame.reset_index and Series.reset_index."""
        if level is not None and not isinstance(level, (tuple, list)):
            level = (level,)
        _check_duplicate_level_names(level, self._index.names)

        # Split the columns in the index into data and index columns
        (
            data_columns,
            index_columns,
            data_names,
            index_names,
        ) = self._index._split_columns_by_levels(level)
        if index_columns:
            index = _index_from_columns(index_columns, name=self._index.name,)
            if isinstance(index, MultiIndex):
                index.names = index_names
            else:
                index.name = index_names[0]
        else:
            index = RangeIndex(len(self))

        if drop:
            return self._data, index

        new_column_data = {}
        for name, col in zip(data_names, data_columns):
            if name == "index" and "index" in self._data:
                name = "level_0"
            name = (
                tuple(
                    name if i == col_level else col_fill
                    for i in range(self._data.nlevels)
                )
                if self._data.multiindex
                else name
            )
            new_column_data[name] = col
        # This is to match pandas where the new data columns are always
        # inserted to the left of existing data columns.
        return (
            ColumnAccessor(
                {**new_column_data, **self._data}, self._data.multiindex
            ),
            index,
        )

    def _first_or_last(
        self, offset, idx: int, op: Callable, side: str, slice_func: Callable
    ) -> "IndexedFrame":
        """Shared code path for ``first`` and ``last``."""
        if not isinstance(self._index, cudf.core.index.DatetimeIndex):
            raise TypeError("'first' only supports a DatetimeIndex index.")
        if not isinstance(offset, str):
            raise NotImplementedError(
                f"Unsupported offset type {type(offset)}."
            )

        if len(self) == 0:
            return self.copy()

        pd_offset = pd.tseries.frequencies.to_offset(offset)
        to_search = op(pd.Timestamp(self._index._column[idx]), pd_offset)
        if (
            idx == 0
            and not isinstance(pd_offset, pd.tseries.offsets.Tick)
            and pd_offset.is_on_offset(pd.Timestamp(self._index[0]))
        ):
            # Special handle is required when the start time of the index
            # is on the end of the offset. See pandas gh29623 for detail.
            to_search = to_search - pd_offset.base
            return self.loc[:to_search]
        end_point = int(
            self._index._column.searchsorted(to_search, side=side)[0]
        )
        return slice_func(end_point)

    def first(self, offset):
        """Select initial periods of time series data based on a date offset.

        When having a DataFrame with **sorted** dates as index, this function
        can select the first few rows based on a date offset.

        Parameters
        ----------
        offset: str
            The offset length of the data that will be selected. For intance,
            '1M' will display all rows having their index within the first
            month.

        Returns
        -------
        Series or DataFrame
            A subset of the caller.

        Raises
        ------
        TypeError
            If the index is not a ``DatetimeIndex``

        Examples
        --------
        >>> i = cudf.date_range('2018-04-09', periods=4, freq='2D')
        >>> ts = cudf.DataFrame({'A': [1, 2, 3, 4]}, index=i)
        >>> ts
                    A
        2018-04-09  1
        2018-04-11  2
        2018-04-13  3
        2018-04-15  4
        >>> ts.first('3D')
                    A
        2018-04-09  1
        2018-04-11  2
        """
        return self._first_or_last(
            offset,
            idx=0,
            op=operator.__add__,
            side="left",
            slice_func=lambda i: self.iloc[:i],
        )

    def last(self, offset):
        """Select final periods of time series data based on a date offset.

        When having a DataFrame with **sorted** dates as index, this function
        can select the last few rows based on a date offset.

        Parameters
        ----------
        offset: str
            The offset length of the data that will be selected. For instance,
            '3D' will display all rows having their index within the last 3
            days.

        Returns
        -------
        Series or DataFrame
            A subset of the caller.

        Raises
        ------
        TypeError
            If the index is not a ``DatetimeIndex``

        Examples
        --------
        >>> i = cudf.date_range('2018-04-09', periods=4, freq='2D')
        >>> ts = cudf.DataFrame({'A': [1, 2, 3, 4]}, index=i)
        >>> ts
                    A
        2018-04-09  1
        2018-04-11  2
        2018-04-13  3
        2018-04-15  4
        >>> ts.last('3D')
                    A
        2018-04-13  3
        2018-04-15  4
        """
        return self._first_or_last(
            offset,
            idx=-1,
            op=operator.__sub__,
            side="right",
            slice_func=lambda i: self.iloc[i:],
        )

    @annotate("SAMPLE", color="orange", domain="cudf_python")
    def sample(
        self,
        n=None,
        frac=None,
        replace=False,
        weights=None,
        random_state=None,
        axis=None,
        ignore_index=False,
    ):
        """Return a random sample of items from an axis of object.

        If reproducible results are required, a random number generator may be
        provided via the `random_state` parameter. This function will always
        produce the same sample given an identical `random_state`.

        Notes
        -----
        When sampling from ``axis=0/'index'``, ``random_state`` can be either
        a numpy random state (``numpy.random.RandomState``) or a cupy random
        state (``cupy.random.RandomState``). When a numpy random state is
        used, the output is guaranteed to match the output of the corresponding
        pandas method call, but generating the sample may be slow. If exact
        pandas equivalence is not required, using a cupy random state will
        achieve better performance, especially when sampling large number of
        items. It's advised to use the matching `ndarray` type to the random
        state for the `weights` array.

        Parameters
        ----------
        n : int, optional
            Number of items from axis to return. Cannot be used with `frac`.
            Default = 1 if frac = None.
        frac : float, optional
            Fraction of axis items to return. Cannot be used with n.
        replace : bool, default False
            Allow or disallow sampling of the same row more than once.
            `replace == True` is not supported for axis = 1/"columns".
            `replace == False` is not supported for axis = 0/"index" given
            `random_state` is `None` or a cupy random state, and `weights` is
            specified.
        weights : ndarray-like, optional
            Default `None` for uniform probability distribution over rows to
            sample from. If `ndarray` is passed, the length of `weights` should
            equal to the number of rows to sample from, and will be normalized
            to have a sum of 1. Unlike pandas, index alignment is not currently
            not performed.
        random_state : int, numpy/cupy RandomState, or None, default None
            If None, default cupy random state is chosen.
            If int, the seed for the default cupy random state.
            If RandomState, rows-to-sample are generated from the RandomState.
        axis : {0 or `index`, 1 or `columns`, None}, default None
            Axis to sample. Accepts axis number or name.
            Default is stat axis for given data type
            (0 for Series and DataFrames). Series doesn't support axis=1.
        ignore_index : bool, default False
            If True, the resulting index will be labeled 0, 1, …, n - 1.

        Returns
        -------
        Series or DataFrame
            A new object of same type as caller containing n items
            randomly sampled from the caller object.

        Examples
        --------
        >>> import cudf as cudf
        >>> df = cudf.DataFrame({"a":{1, 2, 3, 4, 5}})
        >>> df.sample(3)
           a
        1  2
        3  4
        0  1

        >>> sr = cudf.Series([1, 2, 3, 4, 5])
        >>> sr.sample(10, replace=True)
        1    4
        3    1
        2    4
        0    5
        0    1
        4    5
        4    1
        0    2
        0    3
        3    2
        dtype: int64

        >>> df = cudf.DataFrame(
        ...     {"a": [1, 2], "b": [2, 3], "c": [3, 4], "d": [4, 5]}
        ... )
        >>> df.sample(2, axis=1)
           a  c
        0  1  3
        1  2  4
        """
        axis = self._get_axis_from_axis_arg(axis)
        size = self.shape[axis]

        # Compute `n` from parameter `frac`.
        if frac is None:
            n = 1 if n is None else n
        else:
            if frac > 1 and not replace:
                raise ValueError(
                    "Replace has to be set to `True` when upsampling the "
                    "population `frac` > 1."
                )
            if n is not None:
                raise ValueError(
                    "Please enter a value for `frac` OR `n`, not both."
                )
            n = int(round(size * frac))

        if n > 0 and size == 0:
            raise ValueError(
                "Cannot take a sample larger than 0 when axis is empty."
            )

        if isinstance(random_state, cp.random.RandomState):
            lib = cp
        elif isinstance(random_state, np.random.RandomState):
            lib = np
        else:
            # Construct random state if `random_state` parameter is None or a
            # seed. By default, cupy random state is used to sample rows
            # and numpy is used to sample columns. This is because row data
            # is stored on device, and the column objects are stored on host.
            lib = cp if axis == 0 else np
            random_state = lib.random.RandomState(seed=random_state)

        # Normalize `weights` array.
        if weights is not None:
            if isinstance(weights, str):
                raise NotImplementedError(
                    "Weights specified by string is unsupported yet."
                )

            if size != len(weights):
                raise ValueError(
                    "Weights and axis to be sampled must be of same length."
                )

            weights = lib.asarray(weights)
            weights = weights / weights.sum()

        if axis == 0:
            return self._sample_axis_0(
                n, weights, replace, random_state, ignore_index
            )
        else:
            if isinstance(random_state, cp.random.RandomState):
                raise ValueError(
                    "Sampling from `axis=1`/`columns` with cupy random state"
                    "isn't supported."
                )
            return self._sample_axis_1(
                n, weights, replace, random_state, ignore_index
            )

    def _sample_axis_0(
        self,
        n: int,
        weights: Optional[ColumnLike],
        replace: bool,
        random_state: Union[np.random.RandomState, cp.random.RandomState],
        ignore_index: bool,
    ):
        try:
            gather_map_array = random_state.choice(
                len(self), size=n, replace=replace, p=weights
            )
        except NotImplementedError as e:
            raise NotImplementedError(
                "Random sampling with cupy does not support these inputs."
            ) from e

        return self._gather(
            cudf.core.column.as_column(gather_map_array),
            keep_index=not ignore_index,
            check_bounds=False,
        )

    def _sample_axis_1(
        self,
        n: int,
        weights: Optional[ColumnLike],
        replace: bool,
        random_state: np.random.RandomState,
        ignore_index: bool,
    ):
        raise NotImplementedError(
            f"Sampling from axis 1 is not implemented for {self.__class__}."
        )

    def _binaryop(
        self,
        other: Any,
        op: str,
        fill_value: Any = None,
        can_reindex: bool = False,
        *args,
        **kwargs,
    ):
        reflect = self._is_reflected_op(op)
        if reflect:
            op = op[:2] + op[3:]
        operands, out_index = self._make_operands_and_index_for_binop(
            other, op, fill_value, reflect, can_reindex
        )
        if operands is NotImplemented:
            return NotImplemented

        return self._from_data(
            ColumnAccessor(type(self)._colwise_binop(operands, op)),
            index=out_index,
        )

    def _make_operands_and_index_for_binop(
        self,
        other: Any,
        fn: str,
        fill_value: Any = None,
        reflect: bool = False,
        can_reindex: bool = False,
        *args,
        **kwargs,
    ) -> Tuple[
        Union[
            Dict[Optional[str], Tuple[ColumnBase, Any, bool, Any]],
            Type[NotImplemented],
        ],
        Optional[cudf.BaseIndex],
    ]:
        raise NotImplementedError(
            "Binary operations are not supported for {self.__class__}"
        )

    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):
        ret = super().__array_ufunc__(ufunc, method, *inputs, **kwargs)
        fname = ufunc.__name__

        if ret is not None:
            # pandas bitwise operations return bools if indexes are misaligned.
            if "bitwise" in fname:
                reflect = self is not inputs[0]
                other = inputs[0] if reflect else inputs[1]
                if isinstance(other, self.__class__) and not self.index.equals(
                    other.index
                ):
                    ret = ret.astype(bool)
            return ret

        # Attempt to dispatch all other functions to cupy.
        cupy_func = getattr(cp, fname)
        if cupy_func:
            if ufunc.nin == 2:
                other = inputs[self is inputs[0]]
                inputs, index = self._make_operands_and_index_for_binop(
                    other, fname
                )
            else:
                # This works for Index too
                inputs = {
                    name: (col, None, False, None)
                    for name, col in self._data.items()
                }
                index = self._index

            data = self._apply_cupy_ufunc_to_operands(
                ufunc, cupy_func, inputs, **kwargs
            )

            out = tuple(self._from_data(out, index=index) for out in data)
            return out[0] if ufunc.nout == 1 else out

        return NotImplemented

    def _append(
        self, other, ignore_index=False, verify_integrity=False, sort=None
    ):
        warnings.warn(
            "append is deprecated and will be removed in a future version. "
            "Use concat instead.",
            FutureWarning,
        )
        if verify_integrity not in (None, False):
            raise NotImplementedError(
                "verify_integrity parameter is not supported yet."
            )

        if is_list_like(other):
            to_concat = [self, *other]
        else:
            to_concat = [self, other]

        return cudf.concat(to_concat, ignore_index=ignore_index, sort=sort)

    def astype(self, dtype, copy=False, errors="raise", **kwargs):
        """Cast the object to the given dtype.

        Parameters
        ----------
        dtype : data type, or dict of column name -> data type
            Use a numpy.dtype or Python type to cast entire DataFrame object to
            the same type. Alternatively, use ``{col: dtype, ...}``, where col
            is a column label and dtype is a numpy.dtype or Python type
            to cast one or more of the DataFrame's columns to
            column-specific types.
        copy : bool, default False
            Return a deep-copy when ``copy=True``. Note by default
            ``copy=False`` setting is used and hence changes to
            values then may propagate to other cudf objects.
        errors : {'raise', 'ignore', 'warn'}, default 'raise'
            Control raising of exceptions on invalid data for provided dtype.

            -   ``raise`` : allow exceptions to be raised
            -   ``ignore`` : suppress exceptions. On error return original
                object.
            -   ``warn`` : prints last exceptions as warnings and
                return original object.
        **kwargs : extra arguments to pass on to the constructor

        Returns
        -------
        DataFrame/Series

        Examples
        --------
        **DataFrame**

        >>> import cudf
        >>> df = cudf.DataFrame({'a': [10, 20, 30], 'b': [1, 2, 3]})
        >>> df
            a  b
        0  10  1
        1  20  2
        2  30  3
        >>> df.dtypes
        a    int64
        b    int64
        dtype: object

        Cast all columns to `int32`:

        >>> df.astype('int32').dtypes
        a    int32
        b    int32
        dtype: object

        Cast `a` to `float32` using a dictionary:

        >>> df.astype({'a': 'float32'}).dtypes
        a    float32
        b      int64
        dtype: object
        >>> df.astype({'a': 'float32'})
              a  b
        0  10.0  1
        1  20.0  2
        2  30.0  3

        **Series**

        >>> import cudf
        >>> series = cudf.Series([1, 2], dtype='int32')
        >>> series
        0    1
        1    2
        dtype: int32
        >>> series.astype('int64')
        0    1
        1    2
        dtype: int64

        Convert to categorical type:

        >>> series.astype('category')
        0    1
        1    2
        dtype: category
        Categories (2, int64): [1, 2]

        Convert to ordered categorical type with custom ordering:

        >>> cat_dtype = cudf.CategoricalDtype(categories=[2, 1], ordered=True)
        >>> series.astype(cat_dtype)
        0    1
        1    2
        dtype: category
        Categories (2, int64): [2 < 1]

        Note that using ``copy=False`` (enabled by default)
        and changing data on a new Series will
        propagate changes:

        >>> s1 = cudf.Series([1, 2])
        >>> s1
        0    1
        1    2
        dtype: int64
        >>> s2 = s1.astype('int64', copy=False)
        >>> s2[0] = 10
        >>> s1
        0    10
        1     2
        dtype: int64
        """
        if errors not in ("ignore", "warn", "raise"):
            raise ValueError("invalid error value specified")
        elif errors == "warn":
            warnings.warn(
                "Specifying errors='warn' is deprecated and will be removed "
                "in a future release.",
                FutureWarning,
            )

        try:
            data = super().astype(dtype, copy, **kwargs)
        except Exception as e:
            if errors == "raise":
                raise e
            elif errors == "warn":
                import traceback

                tb = traceback.format_exc()
                warnings.warn(tb)
            return self

        return self._from_data(data, index=self._index)

    @annotate("INDEXED_FRAME_DROP", color="green", domain="cudf_python")
    def drop(
        self,
        labels=None,
        axis=0,
        index=None,
        columns=None,
        level=None,
        inplace=False,
        errors="raise",
    ):
        """Drop specified labels from rows or columns.

        Remove rows or columns by specifying label names and corresponding
        axis, or by specifying directly index or column names. When using a
        multi-index, labels on different levels can be removed by specifying
        the level.

        Parameters
        ----------
        labels : single label or list-like
            Index or column labels to drop.
        axis : {0 or 'index', 1 or 'columns'}, default 0
            Whether to drop labels from the index (0 or 'index') or
            columns (1 or 'columns').
        index : single label or list-like
            Alternative to specifying axis (``labels, axis=0``
            is equivalent to ``index=labels``).
        columns : single label or list-like
            Alternative to specifying axis (``labels, axis=1``
            is equivalent to ``columns=labels``).
        level : int or level name, optional
            For MultiIndex, level from which the labels will be removed.
        inplace : bool, default False
            If False, return a copy. Otherwise, do operation
            inplace and return None.
        errors : {'ignore', 'raise'}, default 'raise'
            If 'ignore', suppress error and only existing labels are
            dropped.

        Returns
        -------
        DataFrame or Series
            DataFrame or Series without the removed index or column labels.

        Raises
        ------
        KeyError
            If any of the labels is not found in the selected axis.

        See Also
        --------
        DataFrame.loc : Label-location based indexer for selection by label.
        DataFrame.dropna : Return DataFrame with labels on given axis omitted
            where (all or any) data are missing.
        DataFrame.drop_duplicates : Return DataFrame with duplicate rows
            removed, optionally only considering certain columns.
        Series.reindex
            Return only specified index labels of Series
        Series.dropna
            Return series without null values
        Series.drop_duplicates
            Return series with duplicate values removed

        Examples
        --------
        **Series**

        >>> s = cudf.Series([1,2,3], index=['x', 'y', 'z'])
        >>> s
        x    1
        y    2
        z    3
        dtype: int64

        Drop labels x and z

        >>> s.drop(labels=['x', 'z'])
        y    2
        dtype: int64

        Drop a label from the second level in MultiIndex Series.

        >>> midx = cudf.MultiIndex.from_product([[0, 1, 2], ['x', 'y']])
        >>> s = cudf.Series(range(6), index=midx)
        >>> s
        0  x    0
           y    1
        1  x    2
           y    3
        2  x    4
           y    5
        dtype: int64
        >>> s.drop(labels='y', level=1)
        0  x    0
        1  x    2
        2  x    4
        Name: 2, dtype: int64

        **DataFrame**

        >>> import cudf
        >>> df = cudf.DataFrame({"A": [1, 2, 3, 4],
        ...                      "B": [5, 6, 7, 8],
        ...                      "C": [10, 11, 12, 13],
        ...                      "D": [20, 30, 40, 50]})
        >>> df
           A  B   C   D
        0  1  5  10  20
        1  2  6  11  30
        2  3  7  12  40
        3  4  8  13  50

        Drop columns

        >>> df.drop(['B', 'C'], axis=1)
           A   D
        0  1  20
        1  2  30
        2  3  40
        3  4  50
        >>> df.drop(columns=['B', 'C'])
           A   D
        0  1  20
        1  2  30
        2  3  40
        3  4  50

        Drop a row by index

        >>> df.drop([0, 1])
           A  B   C   D
        2  3  7  12  40
        3  4  8  13  50

        Drop columns and/or rows of MultiIndex DataFrame

        >>> midx = cudf.MultiIndex(levels=[['lama', 'cow', 'falcon'],
        ...                              ['speed', 'weight', 'length']],
        ...                      codes=[[0, 0, 0, 1, 1, 1, 2, 2, 2],
        ...                             [0, 1, 2, 0, 1, 2, 0, 1, 2]])
        >>> df = cudf.DataFrame(index=midx, columns=['big', 'small'],
        ...                   data=[[45, 30], [200, 100], [1.5, 1], [30, 20],
        ...                         [250, 150], [1.5, 0.8], [320, 250],
        ...                         [1, 0.8], [0.3, 0.2]])
        >>> df
                         big  small
        lama   speed    45.0   30.0
               weight  200.0  100.0
               length    1.5    1.0
        cow    speed    30.0   20.0
               weight  250.0  150.0
               length    1.5    0.8
        falcon speed   320.0  250.0
               weight    1.0    0.8
               length    0.3    0.2
        >>> df.drop(index='cow', columns='small')
                         big
        lama   speed    45.0
               weight  200.0
               length    1.5
        falcon speed   320.0
               weight    1.0
               length    0.3
        >>> df.drop(index='length', level=1)
                         big  small
        lama   speed    45.0   30.0
               weight  200.0  100.0
        cow    speed    30.0   20.0
               weight  250.0  150.0
        falcon speed   320.0  250.0
               weight    1.0    0.8
        """
        if labels is not None:
            if index is not None or columns is not None:
                raise ValueError(
                    "Cannot specify both 'labels' and 'index'/'columns'"
                )
            target = labels
        elif index is not None:
            target = index
            axis = 0
        elif columns is not None:
            target = columns
            axis = 1
        else:
            raise ValueError(
                "Need to specify at least one of 'labels', "
                "'index' or 'columns'"
            )

        if inplace:
            out = self
        else:
            out = self.copy()

        if axis in (1, "columns"):
            target = _get_host_unique(target)

            _drop_columns(out, target, errors)
        elif axis in (0, "index"):
            dropped = _drop_rows_by_labels(out, target, level, errors)

            if columns is not None:
                columns = _get_host_unique(columns)
                _drop_columns(dropped, columns, errors)

            out._data = dropped._data
            out._index = dropped._index

        if not inplace:
            return out

    @annotate("INDEXED_FRAME_EXPLODE", color="green", domain="cudf_python")
    def _explode(self, explode_column: Any, ignore_index: bool):
        # Helper function for `explode` in `Series` and `Dataframe`, explodes a
        # specified nested column. Other columns' corresponding rows are
        # duplicated. If ignore_index is set, the original index is not
        # exploded and will be replaced with a `RangeIndex`.
        if not is_list_dtype(self._data[explode_column].dtype):
            data = self._data.copy(deep=True)
            idx = None if ignore_index else self._index.copy(deep=True)
            return self.__class__._from_data(data, index=idx)

        explode_column_num = self._column_names.index(explode_column)
        if not ignore_index and self._index is not None:
            explode_column_num += self._index.nlevels

        data, index = libcudf.lists.explode_outer(
            self, explode_column_num, ignore_index
        )
        res = self.__class__._from_data(
            ColumnAccessor(
                data,
                multiindex=self._data.multiindex,
                level_names=self._data._level_names,
            ),
            index=index,
        )

        if not ignore_index and self._index is not None:
            res.index.names = self._index.names
        return res


def _check_duplicate_level_names(specified, level_names):
    """Raise if any of `specified` has duplicates in `level_names`."""
    if specified is None:
        return
    if len(set(level_names)) == len(level_names):
        return
    duplicates = {key for key, val in Counter(level_names).items() if val > 1}

    duplicates_specified = [spec for spec in specified if spec in duplicates]
    if not len(duplicates_specified) == 0:
        # Note: pandas raises first encountered duplicates, cuDF raises all.
        raise ValueError(
            f"The names {duplicates_specified} occurs multiple times, use a"
            " level number"
        )
