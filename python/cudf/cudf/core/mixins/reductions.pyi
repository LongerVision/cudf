# Copyright (c) 2022, NVIDIA CORPORATION.

from typing import Set

class Reducible:
    _SUPPORTED_REDUCTIONS: Set

    def sum(self):
        ...

    def product(self):
        ...

    def min(self):
        ...

    def max(self):
        ...

    def count(self):
        ...

    def any(self):
        ...

    def all(self):
        ...

    def sum_of_squares(self):
        ...

    def mean(self):
        ...

    def var(self):
        ...

    def std(self):
        ...

    def median(self):
        ...

    def argmax(self):
        ...

    def argmin(self):
        ...

    def nunique(self):
        ...

    def nth(self):
        ...

    def collect(self):
        ...

    def prod(self):
        ...

    def idxmin(self):
        ...

    def idxmax(self):
        ...

    def first(self):
        ...

    def last(self):
        ...
