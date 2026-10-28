// test_book_capped.cpp: the capped book (brief section 5, DESIGN.md 4.2).
//
// What it does:
//   Runs the truncation rules on a K = 4, N = 2 book (template arguments, so
//   every build runs them, the sanitizer builds included):
//     * insert into an empty side, fill to K, insert number K + 1 (the worst
//       dropped, truncated set, B the new worst slot)
//     * a NEW worse than B while truncated is ignored; a NEW better than B
//       is inserted and pushes one out
//     * a DELETE removes a slot with B unchanged; a NEW at B's price is then
//       inserted
//     * depth incomplete sets when valid falls below N while truncated and
//       clears when valid reaches N again, or on a clear; its episodes and
//       their duration
//     * a sweep removing several top levels at once
//     * a consistency error at a price better than B, and a CHANGE or
//       DELETE worse than B ignored
//     * bid and offer ordering are mirror images
//
// What it must not do:
//   Depend on the build's N and K.

#include <doctest.h>

#include <cstdint>
#include <vector>

#include "b3/book_capped.hpp"

using b3::book::Side;
using b3::records::BookLevel;

namespace {

using Book = b3::book::CappedBook<4, 2, 2>;   // K = 4, N = 2, two instruments

std::vector<std::int64_t> prices(const Book& b, Side side = Side::bid, std::size_t slot = 0) {
    BookLevel lv[4];
    const std::size_t n = b.top(slot, side, lv, 4);
    std::vector<std::int64_t> out;
    for (std::size_t i = 0; i < n; ++i) out.push_back(lv[i].price);
    return out;
}

void add(Book& b, std::int64_t p, Side side = Side::bid, std::int64_t qty = 1) { b.add(0, side, p, qty, false); }
void del(Book& b, std::int64_t p, Side side = Side::bid, std::int64_t qty = 1) { b.remove(0, side, p, qty, false); }

}  // namespace

TEST_CASE("capped: insert into an empty side, fill to K, insert number K + 1") {
    Book b;
    add(b, 100);
    CHECK(prices(b) == std::vector<std::int64_t>{100});
    add(b, 98);
    add(b, 99);
    add(b, 97);   // K = 4 levels, sorted best first
    CHECK(prices(b) == std::vector<std::int64_t>{100, 99, 98, 97});
    CHECK(!b.truncated(0, Side::bid));
    add(b, 100, Side::bid, 5);   // an existing level: qty and count
    BookLevel lv[1];
    b.top(0, Side::bid, lv, 1);
    CHECK((lv[0].qty == 6 && lv[0].order_count == 2));
    // Number K + 1, in the middle: the worst (97) is dropped, B the new worst.
    b.add(0, Side::bid, 99'5, 1, false);
    CHECK(prices(b) == std::vector<std::int64_t>{995, 100, 99, 98});
    CHECK((b.truncated(0, Side::bid) && b.boundary(0, Side::bid) == 98));
    CHECK(b.stats(Side::bid).truncations == 1);
    CHECK(b.stats(Side::bid).levels_dropped == 1);
    CHECK(b.stats(Side::bid).max_valid == 4);
    // Number K + 1 the worst itself: it is the one dropped.
    Book c;
    for (std::int64_t p : {100, 99, 98, 97, 96}) add(c, p);
    CHECK(prices(c) == std::vector<std::int64_t>{100, 99, 98, 97});
    CHECK((c.truncated(0, Side::bid) && c.boundary(0, Side::bid) == 97));
}

TEST_CASE("capped: worse than B ignored, better than B pushes one out") {
    Book b;
    for (std::int64_t p : {100, 99, 98, 97, 96}) add(b, p);
    REQUIRE(b.boundary(0, Side::bid) == 97);
    add(b, 95);   // worse than B while truncated: the untracked region
    CHECK(prices(b) == std::vector<std::int64_t>{100, 99, 98, 97});
    CHECK(b.stats(Side::bid).ignored == 1);
    add(b, 101);  // better: inserted, 97 pushed out, B = 98
    CHECK(prices(b) == std::vector<std::int64_t>{101, 100, 99, 98});
    CHECK(b.boundary(0, Side::bid) == 98);
    CHECK(b.stats(Side::bid).truncations == 1);   // already truncated: not a new truncation
    CHECK(b.stats(Side::bid).levels_dropped == 2);
}

TEST_CASE("capped: a delete keeps B; then a NEW at B's price is inserted") {
    Book b;
    for (std::int64_t p : {100, 99, 98, 97, 96}) add(b, p);
    del(b, 97);
    CHECK(prices(b) == std::vector<std::int64_t>{100, 99, 98});
    CHECK((b.truncated(0, Side::bid) && b.boundary(0, Side::bid) == 97));
    add(b, 97, Side::bid, 2);   // at B: inserted
    CHECK(prices(b) == std::vector<std::int64_t>{100, 99, 98, 97});
    add(b, 96);                 // worse than B: still ignored
    CHECK(prices(b) == std::vector<std::int64_t>{100, 99, 98, 97});
    CHECK(b.counters().consistency_errors == 0);
}

TEST_CASE("capped: depth incomplete below N while truncated; clears at N or on a clear") {
    Book b;
    for (std::int64_t p : {100, 99, 98, 97, 96}) add(b, p);
    b.set_time(1000);
    del(b, 100);
    del(b, 99);
    CHECK(!b.depth_incomplete(0, Side::bid));   // 2 valid = N
    del(b, 98);
    CHECK(b.depth_incomplete(0, Side::bid));    // 1 < N, truncated
    b.set_time(1500);
    add(b, 99);                                 // valid reaches N again
    CHECK(!b.depth_incomplete(0, Side::bid));
    CHECK(b.truncated(0, Side::bid));           // still truncated: only a clear resets that
    CHECK(b.stats(Side::bid).depth_incomplete == 1);
    CHECK(b.stats(Side::bid).depth_incomplete_ns == 500);
    b.set_time(2000);
    del(b, 99);
    CHECK(b.depth_incomplete(0, Side::bid));
    b.set_time(2600);
    b.clear_side(0, Side::bid);
    CHECK((!b.depth_incomplete(0, Side::bid) && !b.truncated(0, Side::bid) && b.depth(0, Side::bid) == 0));
    CHECK(b.stats(Side::bid).depth_incomplete == 2);
    CHECK(b.stats(Side::bid).depth_incomplete_ns == 1100);
    CHECK(b.stats(Side::bid).depth_incomplete_max_ns == 600);
    // An episode still open at the end is closed by finish().
    for (std::int64_t p : {100, 99, 98, 97, 96}) add(b, p);
    for (std::int64_t p : {100, 99, 98}) del(b, p);
    b.finish(3000);
    CHECK(b.stats(Side::bid).depth_incomplete_ns == 1100 + 400);
}

TEST_CASE("capped: a sweep removes several top levels at once") {
    Book b;
    for (std::int64_t p : {100, 99, 98, 97, 96}) add(b, p);
    for (std::int64_t p : {100, 99, 98}) del(b, p);   // one aggressor takes three levels
    CHECK(prices(b) == std::vector<std::int64_t>{97});
    CHECK(b.depth_incomplete(0, Side::bid));
    BookLevel lv[2];
    CHECK(b.top(0, Side::bid, lv, 2) == 1);           // the first min(N, valid) slots
    CHECK(lv[0].price == 97);
    CHECK(b.depth(0, Side::bid) == 1);
}

TEST_CASE("capped: consistency error better than B; worse than B ignored") {
    Book b;
    for (std::int64_t p : {100, 99, 98, 97, 96}) add(b, p);
    del(b, 99);
    del(b, 99);                    // better than B, should be in the slots, is not
    CHECK(b.counters().consistency_errors == 1);
    b.change(0, Side::bid, 99, 3, 1, false);     // 99 better than B, not there
    CHECK(b.counters().consistency_errors == 2);
    b.change(0, Side::bid, 90, 3, 1, false);     // worse than B: ignored
    del(b, 90);
    CHECK(b.counters().consistency_errors == 2);
    CHECK(b.stats(Side::bid).ignored == 2);
    b.remove(0, Side::bid, 99, 1, true);         // while STALE: counted apart
    CHECK(b.counters().consistency_errors_stale == 1);
    Book fresh;                                  // not truncated: any missing level is an error
    fresh.remove(0, Side::bid, 50, 1, false);
    CHECK(fresh.counters().consistency_errors == 1);
}

TEST_CASE("capped: bid and offer ordering are mirror images") {
    Book b;
    for (std::int64_t p : {100, 102, 101, 103, 104}) add(b, p, Side::offer);
    CHECK(prices(b, Side::offer) == std::vector<std::int64_t>{100, 101, 102, 103});
    CHECK(b.boundary(0, Side::offer) == 103);
    add(b, 105, Side::offer);   // worse than B for an offer: ignored
    add(b, 99, Side::offer);    // better: inserted, 103 out
    CHECK(prices(b, Side::offer) == std::vector<std::int64_t>{99, 100, 101, 102});
    for (std::int64_t p : {100, 98, 99, 97, 96}) add(b, -p);   // bids at the negated prices
    CHECK(prices(b, Side::bid) == std::vector<std::int64_t>{-96, -97, -98, -99});
    CHECK(b.boundary(0, Side::bid) == -99);
    CHECK(prices(b, Side::bid, 1).empty());   // the other instrument untouched
}
