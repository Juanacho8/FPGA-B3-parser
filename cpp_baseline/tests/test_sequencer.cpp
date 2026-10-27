// test_sequencer.cpp: per-channel sequencing on hand-built packets.
//
// What it does:
//   Feeds packets to the Sequencer, which decides each from its header and
//   its first message's header, and checks what it accepts and counts: a
//   normal run, a gap (and where it starts), a duplicate, heartbeats with the
//   right and the wrong nextSeqNo (checked when the Sequence message is
//   dispatched), SequenceReset with the 65534 -> 1 wrap, a SequenceReset
//   that is not first, channels kept apart, and a malformed packet that
//   still counts for sequencing.
//
// What it must not do:
//   Build inputs with the code under test. Offsets are LAYOUTS.md's.

#include <doctest.h>

#include <cstdint>
#include <vector>

#include "b3/packet.hpp"

using Bytes = std::vector<std::uint8_t>;
using b3::packet::Sequencer;

namespace {

void le(Bytes& b, std::uint64_t v, int n) {
    for (int i = 0; i < n; ++i) b.push_back(static_cast<std::uint8_t>(v >> (8 * i)));
}

void msg(Bytes& b, std::uint16_t tid, std::uint16_t block, std::uint32_t value = 0) {
    le(b, 12u + block, 2);
    le(b, 0xEB50, 2);
    le(b, block, 2);
    le(b, tid, 2);
    le(b, 2, 2);
    le(b, 17, 2);
    if (block >= 4) le(b, value, 4);
    for (int i = 4; i < block; ++i) b.push_back(0);
}

enum class Body { order, heartbeat, reset, malformed_after_order, order_then_reset };

Bytes packet(std::uint8_t ch, std::uint16_t ver, std::uint32_t seq, Body body = Body::order,
             std::uint32_t next_seq_no = 0) {
    Bytes b;
    b.push_back(ch);
    b.push_back(0);
    le(b, ver, 2);
    le(b, seq, 4);
    le(b, 0, 8);
    switch (body) {
        case Body::order: msg(b, 50, 72); break;
        case Body::heartbeat: msg(b, 2, 4, next_seq_no); break;
        case Body::reset: msg(b, 1, 0); break;
        case Body::order_then_reset:
            msg(b, 50, 72);
            msg(b, 1, 0);
            break;
        case Body::malformed_after_order:
            msg(b, 50, 72);
            le(b, 0, 2);   // a messageLength of 0: the rest is discarded
            break;
    }
    return b;
}

struct Seq {
    Sequencer s;
    // A heartbeat's Sequence message is checked as the tracker does it on
    // dispatch: here directly, from the value the packet was built with.
    bool feed(const Bytes& p, std::uint32_t heartbeat_next = 0) {
        const auto h = b3::packet::read_header(p.data());
        const bool ok = s.accept(h, p.data(), p.size());
        if (ok && h.seq_num == 0) s.check_heartbeat(h.channel, heartbeat_next);
        return ok;
    }
    const b3::packet::SequencerCounters& c() const { return s.counters(); }
};

}  // namespace

TEST_CASE("normal run: the first packet sets the expected number") {
    Seq q;
    for (std::uint32_t n = 500; n < 510; ++n) CHECK(q.feed(packet(68, 1, n)));
    CHECK(q.c().sequenced == 10);
    CHECK(q.c().gaps == 0);
    CHECK(q.c().duplicates == 0);
}

TEST_CASE("gap: accepted, the difference counted") {
    Seq q;
    CHECK(q.feed(packet(68, 1, 1)));
    CHECK(q.feed(packet(68, 1, 2)));
    CHECK(q.feed(packet(68, 1, 6)));   // 3, 4, 5 missing
    CHECK(q.s.last().kind == b3::packet::SequenceVerdict::Kind::gap);
    CHECK(q.s.last().gap_first == 3);   // the stale point: the first missing number
    CHECK(q.s.last().gap_version == 1);
    CHECK(q.s.last().gap_count == 3);
    CHECK(q.feed(packet(68, 1, 7)));
    CHECK(q.s.last().kind == b3::packet::SequenceVerdict::Kind::next);
    CHECK(q.c().gaps == 3);
    CHECK(q.c().gap_events == 1);
}

TEST_CASE("duplicate: at or below the last accepted, dropped") {
    Seq q;
    CHECK(q.feed(packet(68, 1, 1)));
    CHECK(q.feed(packet(68, 1, 2)));
    CHECK_FALSE(q.feed(packet(68, 1, 2)));   // equal
    CHECK_FALSE(q.feed(packet(68, 1, 1)));   // lower
    CHECK(q.feed(packet(68, 1, 3)));         // the run continues
    CHECK(q.c().duplicates == 2);
    CHECK(q.c().gaps == 0);
}

TEST_CASE("late_after_gap: a packet inside a recorded gap is late, not a duplicate") {
    Seq q;
    CHECK(q.feed(packet(68, 1, 9)));
    CHECK(q.feed(packet(68, 1, 11)));        // gap: 10 missing
    CHECK_FALSE(q.feed(packet(68, 1, 10)));  // 10 arrives late: dropped, counted as late
    CHECK(q.s.last().kind == b3::packet::SequenceVerdict::Kind::late);
    CHECK(q.c().late_after_gap == 1);
    CHECK(q.c().duplicates == 0);
    CHECK_FALSE(q.feed(packet(68, 1, 11)));  // 11 again: a real duplicate
    CHECK(q.c().duplicates == 1);
    CHECK(q.feed(packet(68, 1, 12)));        // the run goes on: late packets touch no state
    CHECK(q.c().gaps == 1);
    CHECK(q.feed(packet(70, 1, 10)));        // another channel: its own first packet
}

TEST_CASE("late_after_gap: only gaps of the same version, the last kRecordedGaps of them") {
    Seq q;
    CHECK(q.feed(packet(68, 1, 1)));
    std::uint32_t n = 1;
    for (std::size_t g = 0; g < Sequencer::kRecordedGaps + 1; ++g) {   // one gap more than remembered
        n += 2;
        CHECK(q.feed(packet(68, 1, n)));
    }
    CHECK_FALSE(q.feed(packet(68, 1, 2)));    // the oldest gap was forgotten: a duplicate
    CHECK(q.c().duplicates == 1);
    CHECK_FALSE(q.feed(packet(68, 1, n - 1))); // the newest is remembered: late
    CHECK(q.c().late_after_gap == 1);
    CHECK(q.feed(packet(68, 1, n + 1)));
    CHECK(q.feed(packet(68, 1, 50, Body::reset)));
    CHECK(q.feed(packet(68, 2, 1)));
    CHECK_FALSE(q.feed(packet(68, 2, 1)));     // new version: no gap of version 2 recorded
    CHECK(q.c().duplicates == 2);
}

TEST_CASE("channels are sequenced independently") {
    Seq q;
    CHECK(q.feed(packet(68, 1, 10)));
    CHECK(q.feed(packet(70, 1, 500)));
    CHECK(q.feed(packet(68, 1, 11)));
    CHECK(q.feed(packet(70, 1, 501)));
    CHECK(q.c().gaps == 0);
}

TEST_CASE("heartbeats: not sequenced, nextSeqNo checked") {
    Seq q;
    CHECK(q.feed(packet(68, 1, 0, Body::heartbeat, 5), 5));   // before any packet: nothing to check
    CHECK(q.feed(packet(68, 1, 7)));
    CHECK(q.feed(packet(68, 1, 0, Body::heartbeat, 8), 8));   // right
    CHECK(q.c().heartbeat_mismatches == 0);
    CHECK(q.feed(packet(68, 1, 0, Body::heartbeat, 9), 9));   // wrong
    CHECK(q.feed(packet(68, 1, 0, Body::heartbeat, 8), 8));   // heartbeats do not move the number
    CHECK(q.feed(packet(68, 1, 8)));
    CHECK(q.c().heartbeats == 4);
    CHECK(q.c().heartbeat_mismatches == 1);
    CHECK(q.c().sequenced == 2);
    CHECK(q.c().gaps == 0);
}

TEST_CASE("SequenceReset: accepted unchecked, then version + 1 from 1") {
    Seq q;
    CHECK(q.feed(packet(68, 3, 100)));
    CHECK(q.feed(packet(68, 3, 50, Body::reset)));   // its own number and version: not checked
    CHECK(q.feed(packet(68, 1, 0, Body::heartbeat, 1), 1));   // after a reset the next number is 1
    CHECK(q.feed(packet(68, 4, 1)));
    CHECK(q.feed(packet(68, 4, 2)));
    CHECK(q.c().resets == 1);
    CHECK(q.c().reset_mismatches == 0);
    CHECK(q.c().heartbeat_mismatches == 0);
    CHECK(q.c().duplicates == 0);
    CHECK(q.c().gaps == 0);
    CHECK_FALSE(q.feed(packet(68, 4, 1)));   // the new version's own duplicate
}

TEST_CASE("SequenceReset wraps from version 65534 to 1") {
    Seq q;
    CHECK(q.feed(packet(68, 65534, 9)));
    CHECK(q.feed(packet(68, 65534, 10, Body::reset)));
    CHECK(q.feed(packet(68, 1, 1)));
    CHECK(q.c().reset_mismatches == 0);
    CHECK(q.c().version_changes == 0);

    Seq bad;                                    // 65535 instead of 1: counted
    CHECK(bad.feed(packet(68, 65534, 9)));
    CHECK(bad.feed(packet(68, 65534, 10, Body::reset)));
    CHECK(bad.feed(packet(68, 65535, 1)));
    CHECK(bad.c().reset_mismatches == 1);
}

TEST_CASE("heartbeat_without_sequence: a heartbeat whose first message is not a Sequence") {
    Seq q;
    CHECK(q.feed(packet(68, 1, 3)));
    CHECK(q.feed(packet(68, 1, 0, Body::heartbeat, 4), 4));
    CHECK(q.c().heartbeat_without_sequence == 0);
    CHECK(q.feed(packet(68, 1, 0, Body::order)));      // sequenceNumber 0, an Order inside
    CHECK(q.c().heartbeat_without_sequence == 1);
    Bytes header_only = packet(68, 1, 0, Body::order);
    header_only.resize(16);
    CHECK(q.feed(header_only));                        // no message at all
    CHECK(q.c().heartbeat_without_sequence == 2);
}

TEST_CASE("a SequenceReset that is not the first message does not make a reset packet") {
    Seq q;
    CHECK(q.feed(packet(68, 1, 1)));
    CHECK(q.feed(packet(68, 1, 2, Body::order_then_reset)));   // an ordinary packet
    CHECK(q.c().resets == 0);
    CHECK(q.feed(packet(68, 1, 3)));
    CHECK(q.c().gaps == 0);
    CHECK(q.c().reset_mismatches == 0);
}

TEST_CASE("after a SequenceReset, a later first number is a gap") {
    Seq q;
    CHECK(q.feed(packet(68, 2, 40)));
    CHECK(q.feed(packet(68, 2, 41, Body::reset)));
    CHECK(q.feed(packet(68, 3, 4)));   // 1, 2, 3 missing
    CHECK(q.c().reset_mismatches == 1);
    CHECK(q.c().gaps == 3);
}

TEST_CASE("version change without a SequenceReset is counted and followed") {
    Seq q;
    CHECK(q.feed(packet(68, 1, 5)));
    CHECK(q.feed(packet(68, 2, 1)));
    CHECK(q.feed(packet(68, 2, 2)));
    CHECK(q.c().version_changes == 1);
    CHECK(q.c().gaps == 0);
}

TEST_CASE("a malformed packet still counts for sequencing") {
    Seq q;
    CHECK(q.feed(packet(68, 1, 1)));
    CHECK(q.feed(packet(68, 1, 2, Body::malformed_after_order)));
    CHECK(q.feed(packet(68, 1, 3)));
    CHECK(q.c().gaps == 0);
    CHECK(q.c().sequenced == 3);
}

TEST_CASE("short datagram never reaches the sequencer") {
    b3::packet::PacketDecoder<> dec;
    b3::packet::PacketHeader h;
    const Bytes good = packet(68, 1, 1);
    REQUIRE(dec.decode(good.data(), good.size(), h));
    const Bytes tiny(10, 0x44);
    CHECK_FALSE(dec.decode(tiny.data(), tiny.size(), h));
    const Bytes next = packet(68, 1, 2);
    CHECK(dec.decode(next.data(), next.size(), h));
    CHECK(dec.sequencer().counters().gaps == 0);
    CHECK(dec.sequencer().counters().sequenced == 2);
    CHECK(dec.counters().short_datagrams == 1);
}
