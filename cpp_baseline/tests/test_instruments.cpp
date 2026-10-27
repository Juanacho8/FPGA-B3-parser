// test_instruments.cpp: the instrument table's loader and lookup.
//
// What it does:
//   Parses table text: comments, blank lines, lookup order, and each load
//   error (bad line, channel above 255, duplicate securityID, more lines
//   than MAX_INSTRUMENTS).
//
// What it must not do:
//   Hardcode MAX_INSTRUMENTS: it comes from the build (book_params.hpp).

#include <doctest.h>

#include <string>

#include "b3/instruments.hpp"

using b3::instruments::LoadError;
using b3::instruments::Table;

TEST_CASE("table: comments, blank lines, lookup") {
    const auto t = Table::parse("# securityID channel\n\n200000001 68  # WIN\n  200000002\t68\n"
                                "100000001 70\n");
    REQUIRE(t.size() == 3);
    CHECK(t.find(200000001) == 0);
    CHECK(t.find(100000001) == 2);
    CHECK(t[1].channel == 68);
    CHECK(t.find(999) == Table::kNotFound);
    CHECK(Table::parse("# nothing\n").size() == 0);
}

TEST_CASE("table: load errors name the line") {
    CHECK_THROWS_WITH_AS(Table::parse("1 68\nWIN 68\n"), doctest::Contains("instruments:2: expected"),
                         LoadError);
    CHECK_THROWS_WITH_AS(Table::parse("1\n"), doctest::Contains("expected 'securityID channel'"),
                         LoadError);
    CHECK_THROWS_WITH_AS(Table::parse("1 68 9\n"), doctest::Contains("expected"), LoadError);
    CHECK_THROWS_WITH_AS(Table::parse("1 300\n"), doctest::Contains("channel 300 is above 255"),
                         LoadError);
    CHECK_THROWS_WITH_AS(Table::parse("5 68\n6 68\n5 70\n"),
                         doctest::Contains("instruments:3: securityID 5 is already"), LoadError);
    std::string full;
    for (std::size_t i = 0; i < b3::kMaxInstruments; ++i) full += std::to_string(1000 + i) + " 68\n";
    CHECK(Table::parse(full).size() == b3::kMaxInstruments);
    CHECK_THROWS_WITH_AS(Table::parse(full + "1 70\n"), doctest::Contains("more than MAX_INSTRUMENTS"),
                         LoadError);
    CHECK_THROWS_AS(Table::load("/nonexistent/instruments.txt"), LoadError);
}
