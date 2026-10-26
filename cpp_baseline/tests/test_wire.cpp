// test_wire.cpp: wire.hpp loads, both byte orders, every width, unaligned.
//
// What it does:
//   Reads known byte patterns at an odd offset and checks the values, for
//   little- and big-endian, 8 to 64 bits, signed and unsigned.
//
// What it must not do:
//   Compute expected values with the code under test.

#include <doctest.h>

#include <array>
#include <cstdint>

#include "b3/wire.hpp"

using b3::wire::load_be;
using b3::wire::load_le;

namespace {
// Offset 1 makes every multi-byte load unaligned.
constexpr std::array<std::uint8_t, 9> kBytes{0xEE, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x88};
const std::uint8_t* at1() { return kBytes.data() + 1; }
}  // namespace

TEST_CASE("little-endian loads") {
    CHECK(load_le<std::uint8_t>(at1()) == 0x01);
    CHECK(load_le<std::uint16_t>(at1()) == 0x0201);
    CHECK(load_le<std::uint32_t>(at1()) == 0x04030201u);
    CHECK(load_le<std::uint64_t>(at1()) == 0x8807060504030201ull);
}

TEST_CASE("big-endian loads") {
    CHECK(load_be<std::uint8_t>(at1()) == 0x01);
    CHECK(load_be<std::uint16_t>(at1()) == 0x0102);
    CHECK(load_be<std::uint32_t>(at1()) == 0x01020304u);
    CHECK(load_be<std::uint64_t>(at1()) == 0x0102030405060788ull);
}

TEST_CASE("signed loads keep two's complement") {
    constexpr std::array<std::uint8_t, 8> min64{0, 0, 0, 0, 0, 0, 0, 0x80};
    CHECK(load_le<std::int64_t>(min64.data()) == INT64_MIN);
    CHECK(load_be<std::int64_t>(min64.data()) == 128);
    constexpr std::array<std::uint8_t, 4> minus2{0xFE, 0xFF, 0xFF, 0xFF};
    CHECK(load_le<std::int32_t>(minus2.data()) == -2);
    CHECK(load_le<std::int16_t>(minus2.data()) == -2);
    CHECK(load_be<std::int16_t>(minus2.data()) == -257);
    CHECK(load_le<std::int8_t>(minus2.data()) == -2);
}

TEST_CASE("byteswap") {
    CHECK(b3::wire::byteswap<std::uint16_t>(0x1234) == 0x3412);
    CHECK(b3::wire::byteswap<std::uint32_t>(0x12345678u) == 0x78563412u);
    CHECK(b3::wire::byteswap<std::uint64_t>(0x0102030405060708ull) == 0x0807060504030201ull);
    static_assert(b3::wire::byteswap<std::uint8_t>(0xAB) == 0xAB);
}
