/*
 * Copyright (c) 2017-2022, NVIDIA CORPORATION.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#pragma once

#include <cstddef>

#include <cudf/column/column_device_view.cuh>
#include <cudf/detail/utilities/assert.cuh>
#include <cudf/fixed_point/fixed_point.hpp>
#include <cudf/hashing.hpp>
#include <cudf/strings/string_view.cuh>
#include <cudf/types.hpp>

#include <thrust/iterator/reverse_iterator.h>

using hash_value_type = uint32_t;

namespace cudf {
namespace detail {

/**
 * Normalization of floating point NaNs and zeros, passthrough for all other values.
 */
template <typename T>
T __device__ inline normalize_nans_and_zeros(T const& key)
{
  if constexpr (cudf::is_floating_point<T>()) {
    if (std::isnan(key)) {
      return std::numeric_limits<T>::quiet_NaN();
    } else if (key == T{0.0}) {
      return T{0.0};
    }
  }
  return key;
}

/**
 * Modified GPU implementation of
 * https://johnnylee-sde.github.io/Fast-unsigned-integer-to-hex-string/
 * Copyright (c) 2015 Barry Clark
 * Licensed under the MIT license.
 * See file LICENSE for detail or copy at https://opensource.org/licenses/MIT
 */
void __device__ inline uint32ToLowercaseHexString(uint32_t num, char* destination)
{
  // Transform 0xABCD1234 => 0x0000ABCD00001234 => 0x0B0A0D0C02010403
  uint64_t x = num;
  x          = ((x & 0xFFFF0000) << 16) | ((x & 0xFFFF));
  x          = ((x & 0xF0000000F) << 8) | ((x & 0xF0000000F0) >> 4) | ((x & 0xF0000000F00) << 16) |
      ((x & 0xF0000000F000) << 4);

  // Calculate a mask of ascii value offsets for bytes that contain alphabetical hex digits
  uint64_t offsets = (((x + 0x0606060606060606) >> 4) & 0x0101010101010101) * 0x27;

  x |= 0x3030303030303030;
  x += offsets;
  std::memcpy(destination, reinterpret_cast<uint8_t*>(&x), 8);
}

}  // namespace detail
}  // namespace cudf

// MurmurHash3_32 implementation from
// https://github.com/aappleby/smhasher/blob/master/src/MurmurHash3.cpp
//-----------------------------------------------------------------------------
// MurmurHash3 was written by Austin Appleby, and is placed in the public
// domain. The author hereby disclaims copyright to this source code.
// Note - The x86 and x64 versions do _not_ produce the same results, as the
// algorithms are optimized for their respective platforms. You can still
// compile and run any of them on any platform, but your performance with the
// non-native version will be less than optimal.
template <typename Key>
struct MurmurHash3_32 {
  using result_type = hash_value_type;

  MurmurHash3_32() = default;
  constexpr MurmurHash3_32(uint32_t seed) : m_seed(seed) {}

  [[nodiscard]] __device__ inline uint32_t rotl32(uint32_t x, uint32_t r) const
  {
    return __funnelshift_l(x, x, r);  // Equivalent to (x << r) | (x >> (32 - r))
  }

  [[nodiscard]] __device__ inline uint32_t fmix32(uint32_t h) const
  {
    h ^= h >> 16;
    h *= 0x85ebca6b;
    h ^= h >> 13;
    h *= 0xc2b2ae35;
    h ^= h >> 16;
    return h;
  }

  [[nodiscard]] __device__ inline uint32_t getblock32(std::byte const* data,
                                                      cudf::size_type offset) const
  {
    // Read a 4-byte value from the data pointer as individual bytes for safe
    // unaligned access (very likely for string types).
    auto const block = reinterpret_cast<uint8_t const*>(data + offset);
    return block[0] | (block[1] << 8) | (block[2] << 16) | (block[3] << 24);
  }

  // TODO Do we need this operator() and/or compute? Probably not both.
  [[nodiscard]] result_type __device__ inline operator()(Key const& key) const
  {
    return compute(key);
  }

  // compute wrapper for floating point types
  template <typename T, std::enable_if_t<std::is_floating_point_v<T>>* = nullptr>
  hash_value_type __device__ inline compute_floating_point(T const& key) const
  {
    if (key == T{0.0}) {
      return compute(T{0.0});
    } else if (std::isnan(key)) {
      T nan = std::numeric_limits<T>::quiet_NaN();
      return compute(nan);
    } else {
      return compute(key);
    }
  }

  template <typename T>
  result_type __device__ inline compute(T const& key) const
  {
    return compute_bytes(reinterpret_cast<std::byte const*>(&key), sizeof(T));
  }

  result_type __device__ compute_bytes(std::byte const* data, cudf::size_type const len) const
  {
    constexpr cudf::size_type BLOCK_SIZE = 4;
    cudf::size_type const nblocks        = len / BLOCK_SIZE;
    cudf::size_type const tail_offset    = nblocks * BLOCK_SIZE;
    result_type h1                       = m_seed;
    constexpr uint32_t c1                = 0xcc9e2d51;
    constexpr uint32_t c2                = 0x1b873593;
    constexpr uint32_t c3                = 0xe6546b64;
    constexpr uint32_t rot_c1            = 15;
    constexpr uint32_t rot_c2            = 13;

    // Process all four-byte chunks.
    for (cudf::size_type i = 0; i < nblocks; i++) {
      uint32_t k1 = getblock32(data, i * BLOCK_SIZE);
      k1 *= c1;
      k1 = rotl32(k1, rot_c1);
      k1 *= c2;
      h1 ^= k1;
      h1 = rotl32(h1, rot_c2);
      h1 = h1 * 5 + c3;
    }

    // Process remaining bytes that do not fill a four-byte chunk.
    uint32_t k1 = 0;
    switch (len % 4) {
      case 3: k1 ^= std::to_integer<uint8_t>(data[tail_offset + 2]) << 16;
      case 2: k1 ^= std::to_integer<uint8_t>(data[tail_offset + 1]) << 8;
      case 1:
        k1 ^= std::to_integer<uint8_t>(data[tail_offset]);
        k1 *= c1;
        k1 = rotl32(k1, rot_c1);
        k1 *= c2;
        h1 ^= k1;
    };

    // Finalize hash.
    h1 ^= len;
    h1 = fmix32(h1);
    return h1;
  }

 private:
  uint32_t m_seed{cudf::DEFAULT_HASH_SEED};
};

template <>
hash_value_type __device__ inline MurmurHash3_32<bool>::operator()(bool const& key) const
{
  return this->compute(static_cast<uint8_t>(key));
}

template <>
hash_value_type __device__ inline MurmurHash3_32<float>::operator()(float const& key) const
{
  return this->compute_floating_point(key);
}

template <>
hash_value_type __device__ inline MurmurHash3_32<double>::operator()(double const& key) const
{
  return this->compute_floating_point(key);
}

template <>
hash_value_type __device__ inline MurmurHash3_32<cudf::string_view>::operator()(
  cudf::string_view const& key) const
{
  auto const data = reinterpret_cast<std::byte const*>(key.data());
  auto const len  = key.size_bytes();
  return this->compute_bytes(data, len);
}

template <>
hash_value_type __device__ inline MurmurHash3_32<numeric::decimal32>::operator()(
  numeric::decimal32 const& key) const
{
  return this->compute(key.value());
}

template <>
hash_value_type __device__ inline MurmurHash3_32<numeric::decimal64>::operator()(
  numeric::decimal64 const& key) const
{
  return this->compute(key.value());
}

template <>
hash_value_type __device__ inline MurmurHash3_32<numeric::decimal128>::operator()(
  numeric::decimal128 const& key) const
{
  return this->compute(key.value());
}

template <>
hash_value_type __device__ inline MurmurHash3_32<cudf::list_view>::operator()(
  cudf::list_view const& key) const
{
  cudf_assert(false && "List column hashing is not supported");
  return 0;
}

template <>
hash_value_type __device__ inline MurmurHash3_32<cudf::struct_view>::operator()(
  cudf::struct_view const& key) const
{
  cudf_assert(false && "Direct hashing of struct_view is not supported");
  return 0;
}

template <typename Key>
struct SparkMurmurHash3_32 {
  using result_type = hash_value_type;

  SparkMurmurHash3_32() = default;
  constexpr SparkMurmurHash3_32(uint32_t seed) : m_seed(seed) {}

  [[nodiscard]] __device__ inline uint32_t rotl32(uint32_t x, uint32_t r) const
  {
    return __funnelshift_l(x, x, r);  // Equivalent to (x << r) | (x >> (32 - r))
  }

  __device__ inline uint32_t fmix32(uint32_t h) const
  {
    h ^= h >> 16;
    h *= 0x85ebca6b;
    h ^= h >> 13;
    h *= 0xc2b2ae35;
    h ^= h >> 16;
    return h;
  }

  result_type __device__ inline operator()(Key const& key) const { return compute(key); }

  // compute wrapper for floating point types
  template <typename T, std::enable_if_t<std::is_floating_point_v<T>>* = nullptr>
  hash_value_type __device__ inline compute_floating_point(T const& key) const
  {
    if (std::isnan(key)) {
      T nan = std::numeric_limits<T>::quiet_NaN();
      return compute(nan);
    } else {
      return compute(key);
    }
  }

  template <typename T>
  result_type __device__ inline compute(T const& key) const
  {
    return compute_bytes(reinterpret_cast<std::byte const*>(&key), sizeof(T));
  }

  [[nodiscard]] __device__ inline uint32_t getblock32(std::byte const* data,
                                                      cudf::size_type offset) const
  {
    // Individual byte reads for unaligned accesses (very likely for strings)
    auto block = reinterpret_cast<uint8_t const*>(data + offset);
    return block[0] | (block[1] << 8) | (block[2] << 16) | (block[3] << 24);
  }

  result_type __device__ compute_bytes(std::byte const* data, cudf::size_type const len) const
  {
    constexpr cudf::size_type BLOCK_SIZE = 4;
    cudf::size_type const nblocks        = len / BLOCK_SIZE;
    result_type h1                       = m_seed;
    constexpr uint32_t c1                = 0xcc9e2d51;
    constexpr uint32_t c2                = 0x1b873593;
    constexpr uint32_t c3                = 0xe6546b64;
    constexpr uint32_t rot_c1            = 15;
    constexpr uint32_t rot_c2            = 13;

    // Process all four-byte chunks.
    for (cudf::size_type i = 0; i < nblocks; i++) {
      uint32_t k1 = getblock32(data, i * BLOCK_SIZE);
      k1 *= c1;
      k1 = rotl32(k1, rot_c1);
      k1 *= c2;
      h1 ^= k1;
      h1 = rotl32(h1, rot_c2);
      h1 = h1 * 5 + c3;
    }

    // Process remaining bytes that do not fill a four-byte chunk using Spark's approach
    // (does not conform to normal MurmurHash3).
    for (cudf::size_type i = nblocks * 4; i < len; i++) {
      // We require a two-step cast to get the k1 value from the byte. First,
      // we must cast to a signed int8_t. Then, the sign bit is preserved when
      // casting to uint32_t under 2's complement. Java preserves the
      // signedness when casting byte-to-int, but C++ does not.
      uint32_t k1 = static_cast<uint32_t>(std::to_integer<int8_t>(data[i]));
      k1 *= c1;
      k1 = rotl32(k1, rot_c1);
      k1 *= c2;
      h1 ^= k1;
      h1 = rotl32(h1, rot_c2);
      h1 = h1 * 5 + c3;
    }

    // Finalize hash.
    h1 ^= len;
    h1 = fmix32(h1);
    return h1;
  }

 private:
  uint32_t m_seed{cudf::DEFAULT_HASH_SEED};
};

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<bool>::operator()(bool const& key) const
{
  return this->compute<uint32_t>(key);
}

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<int8_t>::operator()(int8_t const& key) const
{
  return this->compute<uint32_t>(key);
}

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<uint8_t>::operator()(uint8_t const& key) const
{
  return this->compute<uint32_t>(key);
}

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<int16_t>::operator()(int16_t const& key) const
{
  return this->compute<uint32_t>(key);
}

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<uint16_t>::operator()(
  uint16_t const& key) const
{
  return this->compute<uint32_t>(key);
}

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<float>::operator()(float const& key) const
{
  return this->compute_floating_point(key);
}

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<double>::operator()(double const& key) const
{
  return this->compute_floating_point(key);
}

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<cudf::string_view>::operator()(
  cudf::string_view const& key) const
{
  auto const data = reinterpret_cast<std::byte const*>(key.data());
  auto const len  = key.size_bytes();
  return this->compute_bytes(data, len);
}

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<numeric::decimal32>::operator()(
  numeric::decimal32 const& key) const
{
  return this->compute<uint64_t>(key.value());
}

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<numeric::decimal64>::operator()(
  numeric::decimal64 const& key) const
{
  return this->compute<uint64_t>(key.value());
}

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<numeric::decimal128>::operator()(
  numeric::decimal128 const& key) const
{
  // Generates the Spark MurmurHash3 hash value, mimicking the conversion:
  // java.math.BigDecimal.valueOf(unscaled_value, _scale).unscaledValue().toByteArray()
  // https://github.com/apache/spark/blob/master/sql/catalyst/src/main/scala/org/apache/spark/sql/catalyst/expressions/hash.scala#L381
  __int128_t const val               = key.value();
  constexpr cudf::size_type key_size = sizeof(__int128_t);
  std::byte const* data              = reinterpret_cast<std::byte const*>(&val);

  // Small negative values start with 0xff..., small positive values start with 0x00...
  bool const is_negative     = val < 0;
  std::byte const zero_value = is_negative ? std::byte{0xff} : std::byte{0x00};

  // If the value can be represented with a shorter than 16-byte integer, the
  // leading bytes of the little-endian value are truncated and are not hashed.
  auto const reverse_begin = thrust::reverse_iterator(data + key_size);
  auto const reverse_end   = thrust::reverse_iterator(data);
  auto const first_nonzero_byte =
    thrust::find_if_not(thrust::seq, reverse_begin, reverse_end, [zero_value](std::byte const& v) {
      return v == zero_value;
    }).base();
  // Max handles special case of 0 and -1 which would shorten to 0 length otherwise
  cudf::size_type length =
    std::max(1, static_cast<cudf::size_type>(thrust::distance(data, first_nonzero_byte)));

  // Preserve the 2's complement sign bit by adding a byte back on if necessary.
  // e.g. 0x0000ff would shorten to 0x00ff. The 0x00 byte is retained to
  // preserve the sign bit, rather than leaving an "f" at the front which would
  // change the sign bit. However, 0x00007f would shorten to 0x7f. No extra byte
  // is needed because the leftmost bit matches the sign bit. Similarly for
  // negative values: 0xffff00 --> 0xff00 and 0xffff80 --> 0x80.
  if ((length < key_size) && (is_negative ^ bool(data[length - 1] & std::byte{0x80}))) { ++length; }

  // Convert to big endian by reversing the range of nonzero bytes. Only those bytes are hashed.
  __int128_t big_endian_value = 0;
  auto big_endian_data        = reinterpret_cast<std::byte*>(&big_endian_value);
  thrust::reverse_copy(thrust::seq, data, data + length, big_endian_data);
  return this->compute_bytes(big_endian_data, length);
}

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<cudf::list_view>::operator()(
  cudf::list_view const& key) const
{
  cudf_assert(false && "List column hashing is not supported");
  return 0;
}

template <>
hash_value_type __device__ inline SparkMurmurHash3_32<cudf::struct_view>::operator()(
  cudf::struct_view const& key) const
{
  cudf_assert(false && "Direct hashing of struct_view is not supported");
  return 0;
}

/**
 * @brief  This hash function simply returns the value that is asked to be hash
 * reinterpreted as the result_type of the functor.
 */
template <typename Key>
struct IdentityHash {
  using result_type = hash_value_type;
  IdentityHash()    = default;
  constexpr IdentityHash(uint32_t seed) : m_seed(seed) {}

  template <typename return_type = result_type>
  constexpr std::enable_if_t<!std::is_arithmetic_v<Key>, return_type> operator()(
    Key const& key) const
  {
    cudf_assert(false && "IdentityHash does not support this data type");
    return 0;
  }

  template <typename return_type = result_type>
  constexpr std::enable_if_t<std::is_arithmetic_v<Key>, return_type> operator()(
    Key const& key) const
  {
    return static_cast<result_type>(key);
  }

 private:
  uint32_t m_seed{cudf::DEFAULT_HASH_SEED};
};

template <typename Key>
using default_hash = MurmurHash3_32<Key>;
