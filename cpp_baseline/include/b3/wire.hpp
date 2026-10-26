// wire.hpp: unaligned loads from a byte buffer, in a stated byte order.
//
// What it does:
//   load_le<T>(p) reads a little-endian integer (B3 fields, pcap headers
//   written on a little-endian machine). load_be<T>(p) reads a big-endian
//   one (Ethernet, IPv4, UDP headers). T is any 8, 16, 32 or 64-bit integer,
//   signed or unsigned. Each load is one memcpy into a local plus, when the
//   host order differs, a byte swap. Compilers turn that into a single load.
//
// What it must not do:
//   Cast a byte pointer to a struct or integer pointer (undefined behaviour:
//   alignment and strict aliasing), or check bounds. Callers check that
//   p .. p + sizeof(T) lies inside the buffer before calling.

#pragma once

#include <bit>
#include <concepts>
#include <cstdint>
#include <cstring>
#include <type_traits>

namespace b3::wire {

template <class T>
concept WireInt = std::integral<T> && !std::same_as<T, bool> &&
                  (sizeof(T) == 1 || sizeof(T) == 2 || sizeof(T) == 4 || sizeof(T) == 8);

static_assert(std::endian::native == std::endian::little || std::endian::native == std::endian::big,
              "mixed-endian hosts are not supported");

// Reverses the byte order of an unsigned integer.
template <std::unsigned_integral U>
constexpr U byteswap(U v) noexcept {
    if constexpr (sizeof(U) == 1) {
        return v;
    } else {
        U out = 0;
        for (std::size_t i = 0; i < sizeof(U); ++i) {
            out = static_cast<U>((out << 8) | (v & 0xFFu));
            v = static_cast<U>(v >> 8);
        }
        return out;
    }
}

namespace detail {

template <WireInt T, std::endian Order>
inline T load(const std::uint8_t* p) noexcept {
    using U = std::make_unsigned_t<T>;
    U raw;
    std::memcpy(&raw, p, sizeof(U));  // the one place bytes become an integer
    if constexpr (Order != std::endian::native) {
        raw = byteswap(raw);
    }
    return std::bit_cast<T>(raw);
}

}  // namespace detail

template <WireInt T>
inline T load_le(const std::uint8_t* p) noexcept {
    return detail::load<T, std::endian::little>(p);
}

template <WireInt T>
inline T load_be(const std::uint8_t* p) noexcept {
    return detail::load<T, std::endian::big>(p);
}

}  // namespace b3::wire
