// test_book_mbo.cpp: the MBO book and its check against the uncapped book.
//
// What it does:
//   * every update (ADD, MODIFY, DELETE) and each clear (CLEAR_SIDE,
//     CLEAR_BOOK, CLEAR_CHANNEL through MboCheck)
//   * every consistency fault reason, split CLEAN / STALE
//   * a MODIFY that keeps priority (same id, same place), and a loss of
//     priority sent as DELETE then ADD with a new id
//   * pool overflow, the hash table's deletion under collisions, peaks
//   * per-price totals equal to the uncapped book's on a small hand-built
//     sequence, through MboCheck with port 1 records
//
// What it must not do:
//   Depend on the build's N and K.

#include <doctest.h>

#include <cstdint>
#include <vector>

#include "b3/book_mbo.hpp"
#include "b3/book_uncapped.hpp"
#include "b3/instruments.hpp"
#include "b3/mbo_check.hpp"
#include "b3/packet.hpp"
#include "b3/records.hpp"
#include "b3/tracker.hpp"

using b3::book::MboBook;
using b3::book::MboFault;
using b3::book::Side;
using b3::records::BookLevel;
using b3::records::EventRecord;

namespace {

std::uint64_t fault(const MboBook& b, MboFault f, bool stale = false) {
    return (stale ? b.counters().faults_stale : b.counters().faults)[static_cast<std::size_t>(f)];
}
std::uint64_t total_faults(const MboBook& b) {
    std::uint64_t n = 0;
    for (auto v : b.counters().faults) n += v;
    return n;
}
std::vector<BookLevel> levels(MboBook& b, Side side, std::size_t slot = 0) {
    std::vector<BookLevel> out(64);
    out.resize(b.levels(slot, side, out.data(), out.size()));
    return out;
}

}  // namespace

TEST_CASE("mbo: ADD, MODIFY, DELETE and per-price totals") {
    MboBook b(2, 16);
    b.add(0, Side::bid, 100, 5, 1, false);
    b.add(0, Side::bid, 100, 3, 2, false);
    b.add(0, Side::bid, 99, 7, 3, false);
    b.add(0, Side::offer, 101, 2, 4, false);
    auto bids = levels(b, Side::bid);
    REQUIRE(bids.size() == 2);
    CHECK((bids[0].price == 100 && bids[0].qty == 8 && bids[0].order_count == 2));
    CHECK((bids[1].price == 99 && bids[1].qty == 7 && bids[1].order_count == 1));
    b.modify(0, Side::bid, 100, 4, 5, 1, false);   // 5 -> 4
    CHECK(levels(b, Side::bid)[0].qty == 7);
    b.remove(0, Side::bid, 100, 3, 2, false);
    bids = levels(b, Side::bid);
    CHECK((bids[0].qty == 4 && bids[0].order_count == 1));
    CHECK(b.resting(0) == 3);
    CHECK(b.peak(0) == 4);
    CHECK(b.counters().peak_orders == 4);
    CHECK(total_faults(b) == 0);
    auto offers = levels(b, Side::offer);
    CHECK((offers.size() == 1 && offers[0].price == 101));
}

TEST_CASE("mbo: a MODIFY keeps priority; a loss of priority is DELETE then ADD with a new id") {
    MboBook b(1, 16);
    b.add(0, Side::bid, 100, 5, 10, false);
    b.add(0, Side::bid, 100, 5, 11, false);
    std::uint64_t ids[4];
    REQUIRE(b.ids(0, Side::bid, ids, 4) == 2);
    const std::uint64_t before[2] = {ids[0], ids[1]};
    b.modify(0, Side::bid, 100, 2, 5, 10, false);   // size down: same id, same place
    REQUIRE(b.ids(0, Side::bid, ids, 4) == 2);
    CHECK((ids[0] == before[0] && ids[1] == before[1]));
    Side side;
    std::int64_t price = 0, qty = 0;
    REQUIRE(b.order(0, 10, side, price, qty));
    CHECK(qty == 2);
    // Size up on order 11: B3 sends DeleteOrder of 11, then Order_MBO NEW as 12.
    b.remove(0, Side::bid, 100, 5, 11, false);
    b.add(0, Side::bid, 100, 9, 12, false);
    CHECK(!b.order(0, 11, side, price, qty));
    REQUIRE(b.order(0, 12, side, price, qty));
    CHECK(qty == 9);
    CHECK(total_faults(b) == 0);
    CHECK(levels(b, Side::bid)[0].qty == 11);
}

TEST_CASE("mbo: every consistency fault, CLEAN and STALE apart") {
    MboBook b(1, 16);
    b.add(0, Side::bid, 100, 5, 1, false);
    b.add(0, Side::bid, 100, 5, 1, false);
    CHECK(fault(b, MboFault::add_duplicate) == 1);
    b.modify(0, Side::bid, 100, 4, 5, 99, false);
    CHECK(fault(b, MboFault::modify_missing) == 1);
    b.modify(0, Side::offer, 100, 4, 5, 1, false);
    CHECK(fault(b, MboFault::modify_side) == 1);
    b.modify(0, Side::offer, 101, 4, 4, 1, false);   // stored now offer 100 x 4 (the message won)
    CHECK(fault(b, MboFault::modify_price) == 1);
    b.modify(0, Side::offer, 101, 3, 9, 1, false);
    CHECK(fault(b, MboFault::modify_prev_qty) == 1);
    b.modify(0, Side::offer, 101, 3, MboBook::kNullPrice, 1, false);   // no previous size
    CHECK(fault(b, MboFault::modify_prev_qty) == 2);
    b.remove(0, Side::offer, 101, 3, 98, false);
    CHECK(fault(b, MboFault::delete_missing) == 1);
    b.add(0, Side::bid, 90, 2, 2, false);
    b.remove(0, Side::offer, 90, 2, 2, false);
    CHECK(fault(b, MboFault::delete_side) == 1);
    b.add(0, Side::bid, 90, 2, 3, false);
    b.remove(0, Side::bid, 91, 2, 3, false);
    CHECK(fault(b, MboFault::delete_price) == 1);
    b.add(0, Side::bid, 90, 2, 4, false);
    b.remove(0, Side::bid, 90, 1, 4, false);
    CHECK(fault(b, MboFault::delete_qty) == 1);
    b.add(0, Side::bid, 90, 2, 5, false);
    b.remove(0, Side::bid, MboBook::kNullPrice, 2, 5, false);   // no price on the wire: not checked
    CHECK(fault(b, MboFault::delete_price) == 1);
    CHECK(total_faults(b) == 10);
    b.remove(0, Side::bid, 90, 2, 777, true);                  // while STALE
    CHECK(fault(b, MboFault::delete_missing, true) == 1);
    CHECK(fault(b, MboFault::delete_missing) == 1);
}

TEST_CASE("mbo: clears, pool overflow, hash deletion with collisions") {
    MboBook b(2, 4);
    b.add(0, Side::bid, 100, 1, 1, false);
    b.add(0, Side::offer, 101, 1, 2, false);
    b.add(1, Side::bid, 50, 1, 1, false);   // the same id on another instrument
    b.clear_side(0, Side::bid);
    CHECK((levels(b, Side::bid).empty() && levels(b, Side::offer).size() == 1));
    b.clear(0);
    CHECK((b.resting(0) == 0 && b.resting(1) == 1));
    Side side;
    std::int64_t price = 0, qty = 0;
    CHECK(b.order(1, 1, side, price, qty));
    for (std::uint64_t id = 10; id < 14; ++id) b.add(0, Side::bid, 100, 1, id, false);
    CHECK(b.counters().pool_overflow == 1);   // 4 orders: 3 fit next to instrument 1's
    // Many inserts and deletes through a small table: every survivor still found.
    MboBook h(1, 64);
    for (std::uint64_t id = 1; id <= 64; ++id) h.add(0, Side::bid, 100, 1, id * 1024, false);
    for (std::uint64_t id = 1; id <= 64; id += 2) h.remove(0, Side::bid, 100, 1, id * 1024, false);
    for (std::uint64_t id = 2; id <= 64; id += 2) CHECK(h.order(0, id * 1024, side, price, qty));
    CHECK(total_faults(h) == 0);
    CHECK(h.resting(0) == 32);
}

TEST_CASE("mbo: levels worse than a price, the host-refill entry point") {
    MboBook b(1, 8);
    for (std::int64_t p : {100, 99, 98, 97}) b.add(0, Side::bid, p, 1, static_cast<std::uint64_t>(p), false);
    BookLevel out[4];
    const std::int64_t boundary = 98;
    CHECK(b.levels(0, Side::bid, out, 4, &boundary) == 1);
    CHECK(out[0].price == 97);
}

namespace {

EventRecord rec(std::uint8_t kind, Side side, std::int64_t price, std::int64_t qty, std::uint64_t id,
                bool closes, std::uint64_t aux = 0, std::uint64_t sid = 200000001) {
    EventRecord r{};
    r.seq_num = 1;
    r.seq_version = 1;
    r.channel = 68;
    r.kind = kind;
    r.flags = closes ? b3::records::flag::END_OF_EVENT : 0;
    r.side = side == Side::bid ? b3::records::side::BID : b3::records::side::OFFER;
    r.instrument_id = sid;
    r.price = price;
    r.qty = qty;
    r.order_id = id;
    r.aux = aux;
    return r;
}

// The uncapped book fed the same records the way book_sink.hpp does.
void feed_levels(b3::book::UncappedBook& u, const EventRecord& r) {
    namespace K = b3::records::kind;
    const Side side = r.side == b3::records::side::OFFER ? Side::offer : Side::bid;
    switch (r.kind) {
        case K::ADD: u.add(0, side, r.price, r.qty, false); break;
        case K::MODIFY: u.change(0, side, r.price, r.qty, static_cast<std::int64_t>(r.aux), false); break;
        case K::DELETE: u.remove(0, side, r.price, r.qty, false); break;
        case K::CLEAR_SIDE: u.clear_side(0, side); break;
        default: u.clear(0); break;
    }
}

}  // namespace

TEST_CASE("mbo check: per-price totals equal the uncapped book's on a hand-built sequence") {
    namespace K = b3::records::kind;
    const auto table = b3::instruments::Table::parse("200000001 68\n");
    b3::packet::Sequencer seq;
    b3::tracker::Tracker tracker(table, seq);
    MboBook mbo(b3::kMaxInstruments, 64);
    b3::book::UncappedBook u(b3::kMaxInstruments, 16);
    b3::book::MboCheck check(table, tracker, mbo, u);
    const std::vector<EventRecord> rs = {
        rec(K::ADD, Side::bid, 100, 5, 1, false), rec(K::ADD, Side::bid, 100, 3, 2, false),
        rec(K::ADD, Side::offer, 102, 4, 3, true),
        rec(K::MODIFY, Side::bid, 100, 1, 1, true, 5),             // partial fill keeps priority
        rec(K::DELETE, Side::bid, 100, 3, 2, false),               // a loss of priority:
        rec(K::ADD, Side::bid, 101, 6, 4, true),                   //   delete, then a new id
        rec(K::CLEAR_SIDE, Side::offer, 0, 0, 0, true),
        rec(K::ADD, Side::offer, 103, 1, 5, true),
        rec(K::CLEAR_BOOK, Side::bid, 0, 0, 0, false), rec(K::ADD, Side::bid, 99, 2, 6, true),
    };
    for (const auto& r : rs) {
        feed_levels(u, r);
        check.on_message(&r, 1);
    }
    CHECK(check.counters().closes == 6);
    CHECK(check.counters().instrument_checks == 6);
    CHECK(check.counters().level_mismatches == 0);
    CHECK(total_faults(mbo) == 0);
    // A wrong level in the uncapped book is found at the next close.
    const EventRecord stray = rec(K::ADD, Side::bid, 98, 1, 7, true);
    u.add(0, Side::bid, 97, 1, false);
    feed_levels(u, stray);
    check.on_message(&stray, 1);
    CHECK(check.counters().level_mismatches == 1);
    REQUIRE(check.kept() == 1);
    CHECK((check.mismatches()[0].level == 2 && check.mismatches()[0].levels.price == 97));
    // CLEAR_CHANNEL: every table instrument of the channel emptied, touched.
    EventRecord reset = rec(K::CLEAR_CHANNEL, Side::bid, 0, 0, 0, false, 0, 0);
    u.clear(0);
    check.on_message(&reset, 1);
    CHECK(mbo.resting(0) == 0);
}
