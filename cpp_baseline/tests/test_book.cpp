// test_book.cpp: the uncapped book and port 2, on hand-built messages.
//
// What it does:
//   * UncappedBook: add (new and existing level), change with prevSize,
//     delete removing a level at count 0, both clears, null prices, level
//     overflow, consistency errors split CLEAN / STALE.
//   * BookSink through the real pipeline (sequencer, walker, tracker, event
//     sink): the touched set across a two-instrument mass cancel (one
//     BOOK_UPDATE each, in order of first touch), CLEAR_BOOK and
//     CLEAR_CHANNEL, STATE_CHANGE on a loss and on recovery, no BOOK_UPDATE
//     while STALE and, with PublishStale, a BOOK_UPDATE marked STALE, the
//     STATUS interval, and output sequence numbers with no holes.
//   * DESIGN.md 4.7's example, parsed from the doc.
//
// What it must not do:
//   Take offsets from layouts.hpp or fields.hpp: they are LAYOUTS.md's.

#include <doctest.h>

#include <cstdint>
#include <cstring>
#include <fstream>
#include <limits>
#include <memory>
#include <sstream>
#include <string>
#include <type_traits>
#include <vector>

#include "b3/book_capped.hpp"
#include "b3/book_sink.hpp"
#include "b3/book_uncapped.hpp"
#include "b3/event_sink.hpp"
#include "b3/instruments.hpp"
#include "b3/packet.hpp"
#include "b3/tracker.hpp"
#include "b3/walker.hpp"

#ifndef B3_REPO_ROOT
#error "B3_REPO_ROOT must point at the repository (CMakeLists.txt sets it)"
#endif

using Bytes = std::vector<std::uint8_t>;
using b3::book::Side;
using b3::book::UncappedBook;
using b3::records::BookLevel;
using b3::records::BookRecord;
namespace RT = b3::records::record_type;
namespace ST = b3::records::book_state;

namespace {

constexpr std::int64_t kNull = std::numeric_limits<std::int64_t>::min();
constexpr std::uint64_t WIN = 200000001, DOL = 200000002;

void put(Bytes& b, std::size_t off, std::uint64_t v, int n) {
    if (b.size() < off + static_cast<std::size_t>(n)) b.resize(off + static_cast<std::size_t>(n), 0);
    for (int i = 0; i < n; ++i) b[off + static_cast<std::size_t>(i)] = static_cast<std::uint8_t>(v >> (8 * i));
}
void le(Bytes& b, std::uint64_t v, int n) { put(b, b.size(), v, n); }

Bytes message(std::uint16_t tid, const Bytes& body) {
    Bytes m;
    le(m, 12 + body.size(), 2); le(m, 0xEB50, 2); le(m, body.size(), 2); le(m, tid, 2); le(m, 2, 2); le(m, 17, 2);
    m.insert(m.end(), body.begin(), body.end());
    return m;
}
Bytes packet(std::uint8_t ch, std::uint32_t seq, std::initializer_list<Bytes> msgs) {
    Bytes p;
    le(p, ch, 1); le(p, 0, 1); le(p, 1, 2); le(p, seq, 4); le(p, 0, 8);
    for (const auto& m : msgs) p.insert(p.end(), m.begin(), m.end());
    return p;
}
// Order_MBO (LAYOUTS.md 7): securityID 0, mEI 8, action 9, type 10, px 12,
// size 20, firm 32, insert time 36, soid 44, rptSeq 52, transactTime 56, prevSize 64.
Bytes order(std::uint64_t sid, std::uint8_t action, char type, std::int64_t px, std::int64_t qty, std::uint64_t soid,
            std::uint32_t rpt, std::uint8_t mei, std::uint64_t time = 7, std::int64_t prev = kNull) {
    Bytes b(72, 0);
    put(b, 0, sid, 8); put(b, 8, mei, 1); put(b, 9, action, 1); put(b, 10, static_cast<unsigned char>(type), 1);
    put(b, 12, static_cast<std::uint64_t>(px), 8); put(b, 20, static_cast<std::uint64_t>(qty), 8); put(b, 32, 15, 4);
    put(b, 36, time, 8); put(b, 44, soid, 8); put(b, 52, rpt, 4); put(b, 56, time, 8);
    put(b, 64, static_cast<std::uint64_t>(prev), 8);
    return message(50, b);
}
// DeleteOrder_MBO: securityID 0, mEI 8, type 10, size 16, soid 24, transactTime 32, rptSeq 40, px 44.
Bytes del(std::uint64_t sid, char type, std::int64_t px, std::int64_t qty, std::uint64_t soid, std::uint32_t rpt,
          std::uint8_t mei) {
    Bytes b(52, 0);
    put(b, 0, sid, 8); put(b, 8, mei, 1); put(b, 10, static_cast<unsigned char>(type), 1);
    put(b, 16, static_cast<std::uint64_t>(qty), 8); put(b, 24, soid, 8); put(b, 32, 9, 8); put(b, 40, rpt, 4);
    put(b, 44, static_cast<std::uint64_t>(px), 8);
    return message(51, b);
}
Bytes empty_book(std::uint64_t sid, std::uint8_t mei, std::uint64_t time) {
    Bytes b(20, 0);
    put(b, 0, sid, 8); put(b, 8, mei, 1); put(b, 12, time, 8);
    return message(9, b);
}
Bytes channel_reset(std::uint8_t mei) {
    Bytes b(12, 0);
    put(b, 0, mei, 1); put(b, 4, 88, 8);
    return message(11, b);
}
Bytes mass_delete(std::uint64_t sid, char type, std::uint32_t rpt, std::uint8_t mei) {
    Bytes b(28, 0);
    put(b, 0, sid, 8); put(b, 8, mei, 1); put(b, 9, 3, 1); put(b, 10, static_cast<unsigned char>(type), 1);
    put(b, 16, 5, 8); put(b, 24, rpt, 4);
    return message(52, b);
}

template <class Book>
Book make_levels() {
    if constexpr (std::is_same_v<Book, UncappedBook>) {
        return UncappedBook(b3::kMaxInstruments, 16);
    } else {
        return Book();
    }
}

// The decode pipeline as b3_decode runs it: tracker, port 1, port 2.
template <class Book = UncappedBook, bool PublishStale = false>
struct Pipeline {
    b3::instruments::Table table;
    b3::packet::PacketDecoder<> decoder;
    b3::walker::Walker walker;
    b3::tracker::Tracker tracker;
    std::vector<b3::records::EventRecord> events_buf;
    b3::event_sink::EventSink events;
    Book levels;
    std::vector<BookRecord> book_buf;
    b3::book::BookSink<Book, PublishStale> book;
    std::vector<Bytes> keep;

    explicit Pipeline(const std::string& table_text, std::uint64_t status_interval = 0, std::size_t capacity = 64,
                      std::uint64_t silence_ns = 0)
        : table(b3::instruments::Table::parse(table_text)),
          tracker(table, decoder.sequencer(), false, silence_ns),
          events_buf(256),
          events(events_buf.data(), events_buf.size(), &tracker),
          levels(make_levels<Book>()),
          book_buf(capacity),
          book(table, tracker, levels, book_buf.data(), book_buf.size(), status_interval) {}

    template <std::uint16_t Id>
    void on(const b3::walker::MessageView& m) {
        tracker.on<Id>(m);
        const std::size_t before = events.size();
        events.on<Id>(m);
        book.on_message(events.data() + before, events.size() - before, tracker.transitions(),
                        tracker.transition_count());
    }
    void on_unknown(const b3::walker::MessageView& m) { tracker.on_unknown(m); }

    void feed(Bytes d, std::uint64_t capture_time = 0) {
        keep.push_back(std::move(d));
        const Bytes& p = keep.back();
        book.begin_datagram(capture_time);          // b3_decode's per-datagram steps
        tracker.on_time(capture_time);
        book.on_transitions(tracker.transitions(), tracker.transition_count());
        if (p.size() >= 16) tracker.saw(p[0], capture_time);
        b3::packet::PacketHeader h;
        if (!decoder.decode(p.data(), p.size(), h)) return;
        const auto& v = decoder.sequencer().last();
        if (v.kind == b3::packet::SequenceVerdict::Kind::gap) {
            tracker.on_loss({h.channel, v.gap_version, v.gap_first});
            book.on_transitions(tracker.transitions(), tracker.transition_count());
        }
        if (walker.walk(h, p.data(), p.size(), *this)) {
            tracker.on_loss({h.channel, h.seq_version, h.seq_num}, b3::records::cause::DISCARDED_TAIL);
            book.on_transitions(tracker.transitions(), tracker.transition_count());
        }
    }
    std::vector<BookRecord> records() const { return {book_buf.begin(), book_buf.begin() + book.size()}; }
};

std::string read_file(const std::string& path) {
    std::ifstream in(path);
    REQUIRE_MESSAGE(in, "cannot read " << path);
    std::stringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

Bytes design_example(const std::string& name) {
    const std::string doc = read_file(std::string(B3_REPO_ROOT) + "/docs/DESIGN.md");
    const auto marker = doc.find("<!-- example:" + name + " -->");
    REQUIRE_MESSAGE(marker != std::string::npos, "example " << name << " not in DESIGN.md");
    const auto start = doc.find("```text\n", marker);
    const auto end = doc.find("```", start + 8);
    std::istringstream lines(doc.substr(start + 8, end - start - 8));
    Bytes out;
    for (std::string line; std::getline(lines, line);) {
        if (line.empty() || line[0] == '#') continue;
        std::istringstream hex(line);
        for (std::string byte; hex >> byte;) out.push_back(static_cast<std::uint8_t>(std::stoul(byte, nullptr, 16)));
    }
    return out;
}

}  // namespace

// ---- UncappedBook ----

TEST_CASE("uncapped book: add, change with prevSize, delete removing a level, best first") {
    UncappedBook b(2, 8);
    b.add(0, Side::bid, 100, 5, false);
    b.add(0, Side::bid, 102, 1, false);
    b.add(0, Side::bid, 100, 2, false);        // existing level: qty 7, count 2
    b.add(0, Side::offer, 105, 4, false);
    b.add(0, Side::offer, 103, 6, false);
    BookLevel lv[4];
    REQUIRE(b.top(0, Side::bid, lv, 4) == 2);
    CHECK(lv[0].price == 102);                 // best bid first
    CHECK((lv[1].price == 100 && lv[1].qty == 7 && lv[1].order_count == 2));
    REQUIRE(b.top(0, Side::offer, lv, 4) == 2);
    CHECK(lv[0].price == 103);                 // best offer first
    b.change(0, Side::bid, 100, 3, 5, false);  // an order of 5 at 100 now 3: qty 7 -> 5
    b.top(0, Side::bid, lv, 4);
    CHECK((lv[1].qty == 5 && lv[1].order_count == 2));
    b.remove(0, Side::bid, 102, 1, false);     // the last order at 102: level gone
    CHECK(b.top(0, Side::bid, lv, 4) == 1);
    CHECK(lv[0].price == 100);
    CHECK(b.counters().consistency_errors == 0);
}

TEST_CASE("uncapped book: clears, null price, overflow, consistency errors split") {
    UncappedBook b(1, 2);
    b.add(0, Side::bid, 100, 1, false);
    b.add(0, Side::bid, 101, 1, false);
    b.add(0, Side::bid, 102, 1, false);        // the side is full (capacity 2)
    CHECK(b.counters().level_overflow == 1);
    b.add(0, Side::bid, kNull, 1, false);
    b.remove(0, Side::bid, kNull, 1, false);
    CHECK(b.counters().null_price_orders == 2);
    b.remove(0, Side::bid, 99, 1, false);       // no such level, CLEAN
    b.change(0, Side::bid, 98, 1, 2, true);     // no such level, STALE
    b.remove(0, Side::bid, 100, 5, false);      // count 0 with qty left
    CHECK(b.counters().consistency_errors == 2);
    CHECK(b.counters().consistency_errors_stale == 1);
    b.add(0, Side::offer, 200, 1, false);
    b.clear_side(0, Side::bid);
    CHECK(b.depth(0, Side::bid) == 0);
    CHECK(b.depth(0, Side::offer) == 1);
    b.clear(0);
    CHECK(b.depth(0, Side::offer) == 0);
    CHECK(b.state_bytes() == 2 * 2 * sizeof(BookLevel) + 2 * sizeof(std::size_t));
}

// ---- Port 2 ----

TEST_CASE("BOOK_UPDATE at the close; one per instrument across a mass cancel, in order of first touch") {
    Pipeline<> p("200000001 68\n200000002 68\n");
    p.feed(packet(68, 1, {order(WIN, 0, '0', 100, 5, 1, 1, 0x00), order(DOL, 0, '1', 200, 3, 2, 1, 0x80)}));
    // A firm's mass cancel: one event of DeleteOrder_MBO across both instruments.
    p.feed(packet(68, 2, {del(DOL, '1', 200, 3, 2, 2, 0x00), del(WIN, '0', 100, 5, 1, 2, 0x80)}));
    const auto rs = p.records();
    REQUIRE(rs.size() == 4);
    CHECK((rs[0].header.record_type == RT::BOOK_UPDATE && rs[0].header.instrument_id == WIN));
    CHECK((rs[0].header.bid_count == 1 && rs[0].bids[0].price == 100 && rs[0].bids[0].qty == 5));
    CHECK(rs[1].header.instrument_id == DOL);
    CHECK((rs[2].header.instrument_id == DOL && rs[3].header.instrument_id == WIN));   // first touch order
    CHECK((rs[2].header.offer_count == 0 && rs[3].header.bid_count == 0));
    CHECK((rs[3].header.seq_num == 2 && rs[3].header.msg_index == 1));                  // the closing message
}

TEST_CASE("CHANGE and CLEAR_SIDE, CLEAR_BOOK, CLEAR_CHANNEL through port 1") {
    Pipeline<> p("200000001 68\n200000002 68\n");
    p.feed(packet(68, 1, {order(WIN, 0, '0', 100, 5, 1, 1, 0x00), order(WIN, 0, '1', 110, 4, 2, 2, 0x00),
                          order(WIN, 1, '0', 100, 2, 1, 3, 0x80, 7, 5)}));   // CHANGE 5 -> 2
    auto rs = p.records();
    REQUIRE(rs.size() == 1);
    CHECK((rs[0].bids[0].qty == 2 && rs[0].offers[0].price == 110));
    p.feed(packet(68, 2, {mass_delete(WIN, '1', 4, 0x80)}));           // CLEAR_SIDE offers
    p.feed(packet(68, 3, {order(DOL, 0, '0', 50, 1, 3, 1, 0x80)}));
    p.feed(packet(68, 4, {empty_book(WIN, 0x80, 9)}));                  // CLEAR_BOOK
    rs = p.records();
    REQUIRE(rs.size() == 4);
    CHECK((rs[1].header.bid_count == 1 && rs[1].header.offer_count == 0));
    CHECK((rs[3].header.instrument_id == WIN && rs[3].header.bid_count == 0));
    // ChannelReset: closes nothing; clears and touches every table book of the
    // channel, published at the channel's next close.
    p.feed(packet(68, 5, {channel_reset(0xA0)}));
    CHECK(p.records().size() == 4);
    p.feed(packet(68, 6, {order(DOL, 0, '0', 51, 1, 4, 2, 0x80)}));
    rs = p.records();
    REQUIRE(rs.size() == 6);
    CHECK((rs[4].header.instrument_id == WIN && rs[4].header.bid_count == 0));   // touched by the reset
    CHECK((rs[5].header.instrument_id == DOL && rs[5].header.bid_count == 1 && rs[5].bids[0].price == 51));
    CHECK((rs[4].header.flags & b3::records::book_flag::RECOVERY) != 0);         // the reset had RecoveryMsg
}

TEST_CASE("STATE_CHANGE on a loss and on recovery; nothing published while STALE") {
    Pipeline<> p("200000001 68\n");
    p.feed(packet(68, 1, {order(WIN, 0, '0', 100, 5, 1, 1, 0x80)}));
    p.feed(packet(68, 3, {order(WIN, 0, '0', 101, 1, 2, 3, 0x80)}));   // gap: 2 missing
    auto rs = p.records();
    REQUIRE(rs.size() == 2);
    CHECK(rs[1].header.record_type == RT::STATE_CHANGE);
    CHECK(rs[1].header.state == ST::STALE);
    CHECK((rs[1].header.seq_num == 2 && rs[1].header.msg_index == 0 && rs[1].header.exch_time == 0));
    CHECK(rs[1].header.cause == b3::records::cause::GAP);
    CHECK((rs[1].header.bid_count == 0 && rs[1].bids[0].price == 0));  // zeros while STALE
    CHECK(rs[0].header.cause == 0);                                     // only STATE_CHANGE has one
    p.feed(packet(68, 4, {order(WIN, 0, '0', 102, 1, 3, 4, 0x80)}));   // still STALE: no BOOK_UPDATE
    CHECK(p.records().size() == 2);
    p.feed(packet(68, 5, {empty_book(WIN, 0x20, 555), order(WIN, 0, '1', 120, 2, 4, 1, 0xA0)}));
    rs = p.records();
    REQUIRE(rs.size() == 4);
    CHECK((rs[2].header.record_type == RT::STATE_CHANGE && rs[2].header.state == ST::CLEAN));
    CHECK((rs[2].header.exch_time == 555 && rs[2].header.bid_count == 0));   // emptied by its EmptyBook
    CHECK(rs[2].header.cause == b3::records::cause::EMPTY_BOOK);
    CHECK((rs[3].header.record_type == RT::BOOK_UPDATE && rs[3].header.offer_count == 1));
    CHECK((rs[3].header.flags & b3::records::book_flag::RECOVERY) != 0);
}

TEST_CASE("STATE_CHANGE cause: discarded tail, then ChannelReset back to CLEAN with the empty book") {
    Pipeline<> p("200000001 68\n");
    Bytes torn = packet(68, 1, {order(WIN, 0, '0', 100, 5, 1, 1, 0x80)});
    const Bytes tail = message(50, Bytes(72, 0));
    torn.insert(torn.end(), tail.begin(), tail.begin() + 20);   // a message cut short: tail discarded
    p.feed(torn);
    auto rs = p.records();
    REQUIRE(rs.size() == 2);
    CHECK((rs[1].header.record_type == RT::STATE_CHANGE && rs[1].header.state == ST::STALE));
    CHECK((rs[1].header.cause == b3::records::cause::DISCARDED_TAIL && rs[1].header.seq_num == 1));
    p.feed(packet(68, 2, {channel_reset(0xA0)}));
    rs = p.records();
    REQUIRE(rs.size() == 3);
    CHECK((rs[2].header.record_type == RT::STATE_CHANGE && rs[2].header.state == ST::CLEAN));
    CHECK((rs[2].header.cause == b3::records::cause::CHANNEL_RESET && rs[2].header.bid_count == 0));
}

TEST_CASE("capped book on port 2: depth-incomplete flags, the first min(N, valid) slots") {
    // K = N: N + 1 bids truncate; a delete then leaves N - 1 < N valid.
    constexpr std::size_t N = b3::kPublishedDepth;
    using Capped = b3::book::CappedBook<N, N>;
    Pipeline<Capped> p("200000001 68\n");
    Bytes pk;
    std::vector<Bytes> msgs;
    for (std::size_t i = 0; i <= N; ++i) {
        msgs.push_back(order(WIN, 0, '0', static_cast<std::int64_t>(100 - i), 1, i + 1, static_cast<std::uint32_t>(i + 1),
                             i == N ? 0x80 : 0));
    }
    Bytes one = packet(68, 1, {});
    for (const auto& m : msgs) one.insert(one.end(), m.begin(), m.end());
    p.feed(one, 10);
    p.feed(packet(68, 2, {del(WIN, '0', 100, 1, 1, static_cast<std::uint32_t>(N + 2), 0x80)}), 20);
    auto rs = p.records();
    REQUIRE(rs.size() == 2);
    CHECK((rs[0].header.flags == 0 && rs[0].header.bid_count == N));       // truncated, N valid: complete
    CHECK(rs[0].bids[N - 1].price == static_cast<std::int64_t>(100 - (N - 1)));
    CHECK(rs[1].header.flags == b3::records::book_flag::DEPTH_INCOMPLETE_BID);
    CHECK((rs[1].header.bid_count == N - 1 && rs[1].bids[0].price == 99));
    p.feed(packet(68, 4, {order(WIN, 0, '1', 200, 1, 50, static_cast<std::uint32_t>(N + 3), 0x80)}), 30);   // gap
    rs = p.records();
    REQUIRE(rs.size() == 3);
    CHECK((rs[2].header.record_type == RT::STATE_CHANGE && rs[2].header.state == ST::STALE));
    CHECK(rs[2].header.flags == b3::records::book_flag::DEPTH_INCOMPLETE_BID);   // on the next record, no record of its own
    CHECK(p.levels.stats(Side::bid).depth_incomplete == 1);
}

TEST_CASE("B3_PUBLISH_STALE: BOOK_UPDATEs while STALE, marked STALE") {
    Pipeline<UncappedBook, true> p("200000001 68\n");
    p.feed(packet(68, 1, {order(WIN, 0, '0', 100, 5, 1, 1, 0x80)}));
    p.feed(packet(68, 2, {order(WIN, 0, '0', 101, 1, 2, 9, 0x80)}));   // rptSeq jump: STALE at this message
    const auto rs = p.records();
    REQUIRE(rs.size() == 3);
    CHECK((rs[1].header.record_type == RT::STATE_CHANGE && rs[1].header.msg_index == 0 && rs[1].header.seq_num == 2));
    CHECK(rs[1].header.cause == b3::records::cause::RPT_SEQ_JUMP);
    CHECK((rs[2].header.record_type == RT::BOOK_UPDATE && rs[2].header.state == ST::STALE));
    CHECK((rs[2].header.bid_count == 2 && rs[2].header.cause == 0));
}

TEST_CASE("STATUS: one for the latest tick due, ticks_skipped; output sequence with no holes") {
    Pipeline<> p("200000001 68\n", 100);
    p.feed(packet(68, 1, {order(WIN, 0, '0', 100, 5, 1, 1, 0x80)}), 1000);   // T0
    p.feed(packet(68, 2, {order(WIN, 0, '0', 100, 5, 2, 2, 0x80)}), 1099);   // no tick yet
    p.feed(Bytes(5, 0), 1100);                                               // a short datagram still ticks
    p.feed(packet(68, 3, {order(WIN, 0, '0', 100, 5, 3, 3, 0x80)}), 1450);   // ticks 1200, 1300, 1400 due
    const auto rs = p.records();
    REQUIRE(rs.size() == 5);
    std::vector<std::uint8_t> types;
    for (std::size_t i = 0; i < rs.size(); ++i) {
        types.push_back(rs[i].header.record_type);
        CHECK(rs[i].header.out_seq == i + 1);
    }
    CHECK(types == std::vector<std::uint8_t>{RT::BOOK_UPDATE, RT::BOOK_UPDATE, RT::STATUS, RT::STATUS, RT::BOOK_UPDATE});
    CHECK((rs[2].header.capture_time == 1100 && rs[2].header.exch_time == 0));       // ticks_skipped 0
    CHECK((rs[3].header.capture_time == 1400 && rs[3].header.exch_time == 2));       // stands for 1200, 1300 too
    CHECK((rs[2].header.instrument_id == 0 && rs[2].header.channel == 0 && rs[2].header.cause == 0));
}

TEST_CASE("silence: a channel quiet past the limit goes STALE when time next advances, once") {
    Pipeline<> p("200000001 68\n100000001 70\n", 0, 64, 3000);
    p.feed(packet(68, 1, {order(WIN, 0, '0', 100, 5, 1, 1, 0x80)}), 1000);
    p.feed(packet(70, 1, {order(100000001, 0, '0', 50, 1, 2, 1, 0x80)}), 1000);
    p.feed(packet(70, 2, {order(100000001, 0, '0', 51, 1, 3, 2, 0x80)}), 3500);    // 68 quiet 2.5: fine
    p.feed(packet(70, 3, {order(100000001, 0, '0', 52, 1, 4, 3, 0x80)}), 4001);    // 68 quiet 3.001: STALE
    CHECK(p.tracker.counters().silences == 1);
    // At 9000 68 is still quiet (no second STATE_CHANGE for it), and 70 itself
    // has been quiet since 4001: it goes STALE before its own packet is read.
    p.feed(packet(70, 4, {order(100000001, 0, '0', 53, 1, 5, 4, 0x80)}), 9000);
    const auto rs = p.records();
    REQUIRE(rs.size() == 6);
    CHECK((rs[3].header.record_type == RT::STATE_CHANGE && rs[3].header.instrument_id == WIN));
    CHECK((rs[3].header.state == ST::STALE && rs[3].header.seq_num == 2 && rs[3].header.capture_time == 4001));
    CHECK((rs[3].header.cause == b3::records::cause::SILENCE && rs[5].header.cause == b3::records::cause::SILENCE));
    CHECK((rs[4].header.record_type == RT::BOOK_UPDATE && rs[4].header.instrument_id == 100000001));
    CHECK((rs[5].header.record_type == RT::STATE_CHANGE && rs[5].header.instrument_id == 100000001));
    CHECK((rs[5].header.seq_num == 4 && rs[5].header.capture_time == 9000));   // 70's next expected number
    CHECK(p.tracker.counters().silences == 2);
    CHECK((p.tracker.state(0).stale && p.tracker.state(1).stale));
}

TEST_CASE("silence: a heartbeat counts as sending; 0 turns the rule off") {
    Bytes hb;
    le(hb, 16, 2); le(hb, 0xEB50, 2); le(hb, 4, 2); le(hb, 2, 2); le(hb, 2, 2); le(hb, 17, 2); le(hb, 2, 4);
    Pipeline<> p("200000001 68\n", 0, 64, 3000);
    p.feed(packet(68, 1, {order(WIN, 0, '0', 100, 5, 1, 1, 0x80)}), 1000);
    p.feed(packet(68, 0, {hb}), 3500);                                            // a heartbeat
    p.feed(packet(68, 2, {order(WIN, 0, '0', 100, 5, 2, 2, 0x80)}), 6000);       // 2.5 after it: fine
    CHECK(p.tracker.counters().silences == 0);
    Pipeline<> off("200000001 68\n", 0, 64, 0);
    off.feed(packet(68, 1, {order(WIN, 0, '0', 100, 5, 1, 1, 0x80)}), 1000);
    off.feed(packet(68, 2, {order(WIN, 0, '0', 100, 5, 2, 2, 0x80)}), 999'000'000);
    CHECK(off.tracker.counters().silences == 0);
}

TEST_CASE("buffer capacity: past it records are counted and out_seq still moves") {
    Pipeline<> p("200000001 68\n", 0, 1);
    p.feed(packet(68, 1, {order(WIN, 0, '0', 100, 5, 1, 1, 0x80), order(WIN, 0, '0', 100, 5, 2, 2, 0x80)}));
    CHECK(p.book.size() == 1);
    CHECK(p.book.counters().overflow == 1);
}

// The example's bytes are N = 5 records: other builds skip it.
TEST_CASE("DESIGN.md 4.7: one record of each type" * doctest::skip(b3::kPublishedDepth != 5)) {
    Pipeline<> p("200000001 68\n", 100'000'000);
    p.feed(packet(68, 1, {order(WIN, 0, '0', 1299800000, 5, 1, 1, 0x00, 999'000'000),
                          order(WIN, 0, '0', 1299750000, 3, 2, 2, 0x80, 999'000'100)}),
           1'000'000'000);
    p.feed(packet(68, 3, {order(WIN, 0, '1', 1299850000, 2, 3, 4, 0x80, 1'049'000'000)}), 1'050'000'000);
    p.feed(packet(68, 4, {empty_book(WIN, 0x20, 1'149'000'000)}), 1'150'000'000);
    const auto rs = p.records();
    Bytes got(rs.size() * sizeof(BookRecord));
    std::memcpy(got.data(), rs.data(), got.size());
    CHECK(got == design_example("book_records"));
}
