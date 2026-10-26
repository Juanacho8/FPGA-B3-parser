// test_build.cpp: step 0 tests, and the test runner's main().
//
// What it does:
//   Proves the test harness runs, and that the book parameters the code sees
//   are the ones CMake was configured with (B3_EXPECT_* come from CMakeLists.txt).
//
// What it must not do:
//   Hardcode parameter values. Expected values always come from the build.

#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest.h>

#include "b3/book_params.hpp"

TEST_CASE("harness runs") {
    CHECK(1 + 1 == 2);
}

TEST_CASE("book parameters match the build configuration") {
    CHECK(b3::kPublishedDepth == B3_EXPECT_PUBLISHED_DEPTH);
    CHECK(b3::kTrackedDepth == B3_EXPECT_TRACKED_DEPTH);
    CHECK(b3::kMaxInstruments == B3_EXPECT_MAX_INSTRUMENTS);
    CHECK((b3::kBookMode == b3::BookMode::Uncapped) == (B3_EXPECT_BOOK_UNCAPPED == 1));
    CHECK(b3::kPublishedDepth <= b3::kTrackedDepth);
}
