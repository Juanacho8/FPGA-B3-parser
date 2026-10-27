// test_tracker.cpp: rptSeq, stale flags, unknown instruments, heartbeats.
//
// What it does:
//   Runs hand-built messages through the Tracker (with a real instrument
//   table and sequencer) and checks: an rptSeq run, an rptSeq jump (that
//   instrument only), rptSeq back to 1 after EmptyBook, a loss marking the
//   whole channel at its point, EmptyBook and ChannelReset clearing stale,
//   the first stale point kept, an unknown securityID counted and not
//   tracked, a heartbeat's nextSeqNo checked on dispatch, a SequenceReset
//   that is not first, and a body fault counted.
//
// What it must not do:
//   Take offsets from layouts.hpp: they are copied from spec/LAYOUTS.md 7.

#include <doctest.h>

#include <cstdint>
#include <vector>

#include "b3/instruments.hpp"
#include "b3/packet.hpp"
#include "b3/tracker.hpp"

using Bytes = std::vector<std::uint8_t>;
using b3::tracker::StalePoint;

namespace {

void put(Bytes& b, std::size_t off, std::uint64_t v, int n) {
    if (b.size() < off + static_cast<std::size_t>(n)) b.resize(off + static_cast<std::size_t>(n), 0);
    for (int i = 0; i < n; ++i) b[off + static_cast<std::size_t>(i)] = static_cast<std::uint8_t>(v >> (8 * i));
}

constexpr std::uint64_t WIN = 200000001, DOL = 200000002, PETR4 = 100000001;

struct Msg {
    Bytes bytes;
    std::uint16_t tid, block;
    b3::packet::PacketHeader packet;
    std::uint16_t index;
    b3::walker::MessageView view() const {
        return {packet, index, tid, 17, block, static_cast<std::uint16_t>(bytes.size()), bytes.data(),
                bytes.data() + 12};
    }
};

Msg make(std::uint16_t tid, Bytes body, std::uint8_t ch, std::uint32_t seq, std::uint16_t index = 0,
         Bytes tail = {}) {
    Msg m{{}, tid, static_cast<std::uint16_t>(body.size()), {ch, 0, 1, seq, 0}, index};
    put(m.bytes, 0, 12 + body.size() + tail.size(), 2);
    m.bytes.resize(12, 0);
    m.bytes.insert(m.bytes.end(), body.begin(), body.end());
    m.bytes.insert(m.bytes.end(), tail.begin(), tail.end());
    return m;
}

// Order_MBO NEW: securityID at 0, rptSeq at 52 (LAYOUTS.md 7).
Msg order(std::uint64_t sid, std::uint32_t rpt, std::uint8_t ch, std::uint32_t seq) {
    Bytes b(72, 0);
    put(b, 0, sid, 8);
    put(b, 52, rpt, 4);
    return make(50, b, ch, seq);
}
Msg empty_book(std::uint64_t sid, std::uint8_t ch, std::uint32_t seq) {
    Bytes b(20, 0);
    put(b, 0, sid, 8);
    put(b, 8, 0x20, 1);
    return make(9, b, ch, seq);
}
Msg channel_reset(std::uint8_t ch, std::uint32_t seq) { return make(11, Bytes(12, 0), ch, seq); }

struct Fixture {
    b3::instruments::Table table = b3::instruments::Table::parse(
        "200000001 68\n200000002 68\n100000001 70\n");
    b3::packet::Sequencer seq;
    b3::tracker::Tracker t{table, seq};

    template <std::uint16_t Id>
    void run(const Msg& m) { t.on<Id>(m.view()); }
    const b3::tracker::InstrumentState& st(std::uint64_t sid) const { return t.state(table.find(sid)); }
};

bool same(const StalePoint& p, std::uint8_t ch, std::uint16_t v, std::uint32_t s) {
    return p.channel == ch && p.version == v && p.seq_num == s;
}

}  // namespace

TEST_CASE("rptSeq run, then a jump marks only that instrument") {
    Fixture f;
    f.run<50>(order(WIN, 10, 68, 1));
    f.run<50>(order(WIN, 11, 68, 2));
    f.run<50>(order(DOL, 4, 68, 2));
    CHECK_FALSE(f.st(WIN).stale);
    f.run<50>(order(WIN, 13, 68, 3));          // 12 lost
    CHECK(f.st(WIN).stale);
    CHECK(f.st(WIN).rpt_seq_gaps == 1);
    CHECK(same(f.st(WIN).first_stale, 68, 1, 3));
    CHECK_FALSE(f.st(DOL).stale);
    f.run<50>(order(WIN, 14, 68, 4));          // continues from 13: no new gap
    CHECK(f.st(WIN).rpt_seq_gaps == 1);
}

TEST_CASE("rptSeq back to 1 after EmptyBook, which also clears stale") {
    Fixture f;
    f.run<50>(order(WIN, 500, 68, 1));
    f.run<50>(order(WIN, 502, 68, 2));         // jump: stale
    REQUIRE(f.st(WIN).stale);
    f.run<9>(empty_book(WIN, 68, 3));
    CHECK_FALSE(f.st(WIN).stale);
    f.run<50>(order(WIN, 1, 68, 3));           // restarts at 1: no gap
    CHECK(f.st(WIN).rpt_seq_gaps == 1);
    f.run<50>(order(WIN, 3, 68, 4));           // after the restart the rule applies again
    CHECK(f.st(WIN).rpt_seq_gaps == 2);
    CHECK(same(f.st(WIN).first_stale, 68, 1, 2));   // the first point is kept
    CHECK(f.st(WIN).stale_events == 2);
}

TEST_CASE("a loss marks every table instrument of its channel; ChannelReset clears them") {
    Fixture f;
    f.t.on_loss({68, 1, 905});
    CHECK(f.st(WIN).stale);
    CHECK(f.st(DOL).stale);
    CHECK_FALSE(f.st(PETR4).stale);            // channel 70
    CHECK(same(f.st(DOL).first_stale, 68, 1, 905));
    f.t.on_loss({68, 1, 990});                 // still stale: first point unchanged
    CHECK(same(f.st(DOL).first_stale, 68, 1, 905));
    CHECK(f.st(DOL).stale_events == 1);
    f.run<11>(channel_reset(68, 1000));
    CHECK_FALSE(f.st(WIN).stale);
    CHECK_FALSE(f.st(DOL).stale);
    CHECK(f.st(WIN).ever_stale);
    CHECK(f.t.counters().losses == 2);
}

TEST_CASE("unknown securityID: counted, not tracked") {
    Fixture f;
    f.run<50>(order(999, 7, 68, 1));
    f.run<50>(order(999, 70, 68, 2));          // a jump, but not tracked
    CHECK(f.t.counters().unknown_instrument_messages == 2);
    for (std::size_t slot = 0; slot < f.table.size(); ++slot) CHECK(f.t.state(slot).rpt_seq_gaps == 0);
    // News with a null securityID (0, market-wide) is not an unknown instrument.
    Bytes news(36, 0);
    f.run<5>(make(5, news, 70, 3, 0, Bytes(6, 0)));
    CHECK(f.t.counters().unknown_instrument_messages == 2);
}

TEST_CASE("heartbeat nextSeqNo checked when its Sequence message is dispatched") {
    Fixture f;
    b3::packet::PacketHeader h{68, 0, 1, 40, 0};
    Bytes payload(16, 0);
    REQUIRE(f.seq.accept(h, payload.data(), payload.size()));   // channel 68 expects 41
    Bytes body(4, 0);
    put(body, 0, 41, 4);
    f.run<2>(make(2, body, 68, 0));
    CHECK(f.seq.counters().heartbeat_mismatches == 0);
    put(body, 0, 44, 4);
    f.run<2>(make(2, body, 68, 0));
    CHECK(f.seq.counters().heartbeat_mismatches == 1);
}

TEST_CASE("SequenceReset not first in its packet is counted") {
    Fixture f;
    f.run<1>(make(1, {}, 68, 7, 0));
    CHECK(f.t.counters().seq_reset_not_first == 0);
    f.run<1>(make(1, {}, 68, 8, 3));
    CHECK(f.t.counters().seq_reset_not_first == 1);
}

TEST_CASE("body fault counted") {
    Fixture f;
    Bytes news(36, 0);
    put(news, 0, PETR4, 8);
    f.run<5>(make(5, news, 70, 1, 0, {1, 0, 'H', 50, 0}));   // text says 50 bytes, none follow
    CHECK(f.t.counters().body_faults[3] == 1);                // var_data_past_end
}
