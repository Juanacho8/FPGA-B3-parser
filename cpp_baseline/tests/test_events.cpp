// test_events.cpp: port 1 records and event closing, on hand-built messages.
//
// What it does:
//   * One test per kind: a hand-built message, its record compared with
//     bytes packed field by field here (independent of records.hpp's struct).
//   * Event closing (DESIGN.md 3.5): a closer on port 1 sets flag bit 0, a
//     non-port-1 closer with an event open gives an END_OF_EVENT marker, a
//     standalone closer gives nothing, a SequenceReset does not close an open
//     event (counted), a duplicate packet and a discarded malformed tail give
//     nothing.
//   * The buffer's capacity counter.
//   * DESIGN.md's hex examples, parsed from the doc as tools/tests does it:
//     the sample packet (from its packet.hex), the trade event closed by
//     ExecutionStatistics (its four messages built here from the fixture's
//     values) and the default events.bin header.
//
// What it must not do:
//   Take offsets from layouts.hpp or fields.hpp: they are LAYOUTS.md's.

#include <doctest.h>

#include <cstdint>
#include <cstring>
#include <fstream>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

#include "b3/event_sink.hpp"
#include "b3/instruments.hpp"
#include "b3/tracker.hpp"
#include "b3/packet.hpp"
#include "b3/records.hpp"
#include "b3/walker.hpp"

using Bytes = std::vector<std::uint8_t>;
using b3::records::EventRecord;
namespace K = b3::records::kind;

#ifndef B3_REPO_ROOT
#error "B3_REPO_ROOT must point at the repository (CMakeLists.txt sets it)"
#endif

namespace {

void put(Bytes& b, std::size_t off, std::uint64_t v, int n) {
    if (b.size() < off + static_cast<std::size_t>(n)) b.resize(off + static_cast<std::size_t>(n), 0);
    for (int i = 0; i < n; ++i) b[off + static_cast<std::size_t>(i)] = static_cast<std::uint8_t>(v >> (8 * i));
}
void le(Bytes& b, std::uint64_t v, int n) { put(b, b.size(), v, n); }

// The 64 bytes DESIGN.md 3.1 says a record is, packed here field by field.
struct Expect {
    std::uint32_t seq_num = 0; std::uint16_t seq_version = 0; std::uint8_t channel = 0, msg_index = 0;
    std::uint8_t kind = 0, flags = 0, side = 0, venue = 0; std::uint32_t rpt_seq = 0;
    std::uint64_t instrument = 0; std::int64_t price = 0, qty = 0;
    std::uint64_t order_id = 0, exch_time = 0, aux = 0;
    Bytes bytes() const {
        Bytes b;
        le(b, seq_num, 4); le(b, seq_version, 2); le(b, channel, 1); le(b, msg_index, 1);
        le(b, kind, 1); le(b, flags, 1); le(b, side, 1); le(b, venue, 1); le(b, rpt_seq, 4);
        le(b, instrument, 8); le(b, static_cast<std::uint64_t>(price), 8); le(b, static_cast<std::uint64_t>(qty), 8);
        le(b, order_id, 8); le(b, exch_time, 8); le(b, aux, 8);
        return b;
    }
};

Bytes bytes_of(const EventRecord& r) {
    Bytes b(sizeof r);
    std::memcpy(b.data(), &r, sizeof r);
    return b;
}

// A message: 12-byte header, then body.
Bytes message(std::uint16_t tid, const Bytes& body, std::uint16_t version = 17, const Bytes& tail = {}) {
    Bytes m;
    le(m, 12 + body.size() + tail.size(), 2);
    le(m, 0xEB50, 2);
    le(m, body.size(), 2);
    le(m, tid, 2);
    le(m, 2, 2);
    le(m, version, 2);
    m.insert(m.end(), body.begin(), body.end());
    m.insert(m.end(), tail.begin(), tail.end());
    return m;
}

Bytes packet(std::uint8_t ch, std::uint32_t seq, std::initializer_list<Bytes> msgs, std::uint16_t ver = 1,
             std::uint64_t sending = 0) {
    Bytes p;
    le(p, ch, 1); le(p, 0, 1); le(p, ver, 2); le(p, seq, 4); le(p, sending, 8);
    for (const auto& m : msgs) p.insert(p.end(), m.begin(), m.end());
    return p;
}

// Runs datagrams through the packet decoder, walker and event sink.
struct Port1 {
    std::vector<EventRecord> buf;
    b3::event_sink::EventSink sink;
    b3::packet::PacketDecoder<> decoder;
    b3::walker::Walker walker;
    std::vector<Bytes> keep;   // the datagrams stay alive with the records' sources
    explicit Port1(std::size_t capacity = 64) : buf(capacity), sink(buf.data(), buf.size()) {}
    void feed(Bytes d) {
        keep.push_back(std::move(d));
        const Bytes& p = keep.back();
        b3::packet::PacketHeader h;
        if (decoder.decode(p.data(), p.size(), h)) walker.walk(h, p.data(), p.size(), sink);
    }
    std::vector<EventRecord> records() const { return {buf.begin(), buf.begin() + sink.size()}; }
};

constexpr std::int64_t kNull64 = std::numeric_limits<std::int64_t>::min();

// Bodies, offsets from LAYOUTS.md 7.
Bytes order(std::uint8_t mei, std::uint8_t action, char type, std::int64_t px, std::int64_t qty, std::uint32_t firm,
            std::uint64_t soid, std::uint32_t rpt, std::uint64_t time, std::int64_t prev, std::size_t size = 72) {
    Bytes b(size, 0);
    put(b, 0, 7, 8); put(b, 8, mei, 1); put(b, 9, action, 1); put(b, 10, static_cast<unsigned char>(type), 1);
    put(b, 12, static_cast<std::uint64_t>(px), 8); put(b, 20, static_cast<std::uint64_t>(qty), 8);
    put(b, 32, firm, 4); put(b, 44, soid, 8); put(b, 52, rpt, 4);
    if (size >= 64) put(b, 56, time, 8);
    if (size >= 72) put(b, 64, static_cast<std::uint64_t>(prev), 8);
    return b;
}
Bytes stats(std::uint8_t mei, std::uint64_t time) {
    Bytes b(52, 0);
    put(b, 0, 7, 8); put(b, 8, mei, 1); put(b, 40, time, 8);
    return b;
}
Bytes summary() {
    Bytes b(64, 0);
    put(b, 0, 7, 8); put(b, 10, 1, 1); put(b, 12, 100, 8); put(b, 20, 5, 8); put(b, 52, 3, 4); put(b, 56, 900, 8);
    return b;
}

std::string read_file(const std::string& path) {
    std::ifstream in(path);
    REQUIRE_MESSAGE(in, "cannot read " << path);
    std::stringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

// The bytes of the ```text block after <!-- example:name --> in DESIGN.md.
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

Bytes concat(const std::vector<EventRecord>& rs) {
    Bytes out;
    for (const auto& r : rs) {
        const auto b = bytes_of(r);
        out.insert(out.end(), b.begin(), b.end());
    }
    return out;
}

}  // namespace

// ---- One test per kind ----

TEST_CASE("ADD and MODIFY (Order_MBO NEW, CHANGE; prevSize null and absent)") {
    Port1 p;
    p.feed(packet(68, 1, {message(50, order(0x80, 0, '0', 1000, 5, 15, 42, 9, 777, kNull64))}));
    p.feed(packet(68, 2, {message(50, order(0x00, 1, '1', 1000, 3, 0, 42, 10, 778, 5))}));
    p.feed(packet(68, 3, {message(50, order(0x80, 1, '1', 1000, 2, 0, 42, 11, 0, 0, 64), 15)}));   // v15: no prevSize
    const auto rs = p.records();
    REQUIRE(rs.size() == 3);
    CHECK(bytes_of(rs[0]) == Expect{1, 1, 68, 0, K::ADD, 0x01, 1, 50, 9, 7, 1000, 5, 42, 777, 15}.bytes());
    CHECK(bytes_of(rs[1]) == Expect{2, 1, 68, 0, K::MODIFY, 0x00, 2, 50, 10, 7, 1000, 3, 42, 778, 5}.bytes());
    CHECK(bytes_of(rs[2]) == Expect{3, 1, 68, 0, K::MODIFY, 0x01, 2, 50, 11, 7, 1000, 2, 42, 0,
                                    static_cast<std::uint64_t>(kNull64)}.bytes());
}

TEST_CASE("DELETE, with mDEntryPx and transactTime absent before v15") {
    Bytes d(52, 0);
    put(d, 0, 7, 8); put(d, 8, 0x80, 1); put(d, 10, '0', 1); put(d, 16, 4, 8); put(d, 24, 42, 8);
    put(d, 32, 555, 8); put(d, 40, 12, 4); put(d, 44, 990, 8);
    Bytes old(32, 0);                    // version 14: blockLength 32, the v15 fields absent
    put(old, 0, 7, 8); put(old, 8, 0x80, 1); put(old, 10, '1', 1); put(old, 16, 4, 8); put(old, 24, 43, 8);
    Port1 p;
    p.feed(packet(68, 1, {message(51, d), message(51, old, 14)}));
    const auto rs = p.records();
    REQUIRE(rs.size() == 2);
    CHECK(bytes_of(rs[0]) == Expect{1, 1, 68, 0, K::DELETE, 0x01, 1, 51, 12, 7, 990, 4, 42, 555, 0}.bytes());
    CHECK(bytes_of(rs[1]) == Expect{1, 1, 68, 1, K::DELETE, 0x01, 2, 51, 0, 7, kNull64, 4, 43, 0, 0}.bytes());
}

TEST_CASE("CLEAR_SIDE (DELETE_THRU only), CLEAR_BOOK, CLEAR_CHANNEL") {
    Bytes mass(28, 0);
    put(mass, 0, 7, 8); put(mass, 8, 0x80, 1); put(mass, 9, 3, 1); put(mass, 10, '1', 1); put(mass, 16, 66, 8);
    put(mass, 24, 5, 4);
    Bytes other = mass;
    put(other, 9, 4, 1);                 // DELETE_FROM: not on port 1
    Bytes empty(20, 0);
    put(empty, 0, 7, 8); put(empty, 8, 0x20, 1); put(empty, 12, 77, 8);
    Bytes reset(12, 0);
    put(reset, 0, 0xA0, 1); put(reset, 4, 88, 8);
    Port1 p;
    p.feed(packet(70, 1, {message(52, mass), message(52, other), message(9, empty), message(11, reset)}));
    const auto rs = p.records();
    REQUIRE(rs.size() == 3);
    CHECK(bytes_of(rs[0]) == Expect{1, 1, 70, 0, K::CLEAR_SIDE, 0x01, 2, 52, 5, 7, 0, 0, 0, 66, 0}.bytes());
    CHECK(bytes_of(rs[1]) == Expect{1, 1, 70, 2, K::CLEAR_BOOK, 0x02, 0, 9, 0, 7, 0, 0, 0, 77, 0}.bytes());
    CHECK(bytes_of(rs[2]) == Expect{1, 1, 70, 3, K::CLEAR_CHANNEL, 0x03, 0, 11, 0, 0, 0, 0, 0, 88, 0}.bytes());
}

TEST_CASE("EXEC_SUMMARY, TRADE, TRADE_BUST") {
    Bytes t(56, 0);
    put(t, 0, 7, 8); put(t, 12, 100, 8); put(t, 20, 5, 8); put(t, 28, 33, 4); put(t, 32, 15, 4); put(t, 36, 14, 4);
    put(t, 44, 901, 8); put(t, 52, 4, 4);
    Bytes b(48, 0);
    put(b, 0, 7, 8); put(b, 8, 0x80, 1); put(b, 12, 100, 8); put(b, 20, 5, 8); put(b, 28, 33, 4);
    put(b, 36, 950, 8); put(b, 44, 6, 4);
    Port1 p;
    p.feed(packet(68, 1, {message(55, summary()), message(53, t), message(57, b)}));
    const auto rs = p.records();
    REQUIRE(rs.size() == 3);
    CHECK(bytes_of(rs[0]) == Expect{1, 1, 68, 0, K::EXEC_SUMMARY, 0, 0, 55, 3, 7, 100, 5, 0, 900, 1}.bytes());
    CHECK(bytes_of(rs[1]) == Expect{1, 1, 68, 1, K::TRADE, 0, 0, 53, 4, 7, 100, 5, 33, 901,
                                    (15ull << 32) | 14}.bytes());
    CHECK(bytes_of(rs[2]) == Expect{1, 1, 68, 2, K::TRADE_BUST, 0x01, 0, 57, 6, 7, 100, 5, 33, 950, 0}.bytes());
}

TEST_CASE("SESSION from SecurityGroupPhase and SecurityStatus, SEQ_RESET") {
    Bytes g(32, 0);
    put(g, 0, 'I', 1); put(g, 1, 'N', 1); put(g, 2, 'D', 1); put(g, 8, 0x80, 1); put(g, 9, 1, 1); put(g, 10, 17, 1);
    put(g, 11, 4, 1); put(g, 12, 20745, 2); put(g, 24, 123, 8);
    Bytes s(36, 0);
    put(s, 0, 7, 8); put(s, 8, 0x80, 1); put(s, 9, 1, 1); put(s, 10, 2, 1); put(s, 11, 255, 1);
    put(s, 12, 20745, 2); put(s, 24, 124, 8); put(s, 32, 8, 4);
    Port1 p;
    p.feed(packet(68, 1, {message(10, g), message(3, s)}));
    p.feed(packet(68, 2, {message(1, {})}));
    const auto rs = p.records();
    REQUIRE(rs.size() == 3);
    const std::uint64_t group_word = 1 | (17ull << 8) | (4ull << 16) | (20745ull << 24) | (std::uint64_t{'I'} << 40) |
                                     (std::uint64_t{'N'} << 48) | (std::uint64_t{'D'} << 56);
    CHECK(bytes_of(rs[0]) == Expect{1, 1, 68, 0, K::SESSION, 0x01, 0, 10, 0, 0, 0, 0, 0, 123, group_word}.bytes());
    const std::uint64_t status_word = 1 | (2ull << 8) | (255ull << 16) | (20745ull << 24);
    CHECK(bytes_of(rs[1]) == Expect{1, 1, 68, 1, K::SESSION, 0x01, 0, 3, 8, 7, 0, 0, 0, 124, status_word}.bytes());
    CHECK(bytes_of(rs[2]) == Expect{2, 1, 68, 0, K::SEQ_RESET, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0}.bytes());
}

TEST_CASE("END_OF_EVENT marker: identity, closer's templateId, time field and flags") {
    Port1 p;
    p.feed(packet(68, 1, {message(55, summary()), message(56, stats(0x80 | 0x10, 2000))}));
    const auto rs = p.records();
    REQUIRE(rs.size() == 2);
    CHECK(bytes_of(rs[1]) == Expect{1, 1, 68, 1, K::END_OF_EVENT, 0x01 | 0x04, 0, 56, 0, 0, 0, 0, 0, 2000, 0}.bytes());
    CHECK(p.sink.counters().markers == 1);
}

// ---- Event closing ----

TEST_CASE("closing: closer on port 1, non-port-1 closer, standalone closer") {
    Port1 p;
    p.feed(packet(68, 1, {message(50, order(0x80, 0, '0', 1, 1, 0, 1, 1, 1, kNull64))}));   // own event
    p.feed(packet(68, 2, {message(56, stats(0x80, 5))}));                                     // standalone: nothing
    p.feed(packet(68, 3, {message(50, order(0x00, 0, '0', 1, 1, 0, 2, 2, 1, kNull64)),
                          message(50, order(0x80, 1, '0', 1, 1, 0, 2, 3, 1, 1))}));           // closer on port 1
    p.feed(packet(68, 4, {message(50, order(0x00, 0, '0', 1, 1, 0, 3, 4, 1, kNull64)),
                          message(56, stats(0x80, 6))}));                                    // marker
    const auto rs = p.records();
    REQUIRE(rs.size() == 5);
    CHECK((rs[0].flags & 1) == 1);
    CHECK((rs[1].flags & 1) == 0);
    CHECK((rs[2].flags & 1) == 1);
    CHECK(rs[3].kind == K::ADD);
    CHECK(rs[4].kind == K::END_OF_EVENT);
    CHECK_FALSE(p.sink.event_open(68));
}

TEST_CASE("closing: channels apart; a SequenceReset arriving with an event open clears it") {
    Port1 p;
    p.feed(packet(68, 1, {message(55, summary())}));
    CHECK(p.sink.event_open(68));
    CHECK_FALSE(p.sink.event_open(70));
    p.feed(packet(70, 1, {message(56, stats(0x80, 5))}));     // other channel: standalone, nothing
    CHECK(p.records().size() == 1);
    p.feed(packet(68, 2, {message(1, {})}));                   // reset packet
    CHECK_FALSE(p.sink.event_open(68));                        // cleared, nothing emitted but SEQ_RESET
    CHECK(p.sink.counters().resets_with_event_open == 1);
    p.feed(packet(68, 1, {message(56, stats(0x80, 6))}, 2));   // standalone now: no marker
    const auto rs = p.records();
    REQUIRE(rs.size() == 2);
    CHECK(rs[1].kind == K::SEQ_RESET);
}

TEST_CASE("closing: a ChannelReset arriving with an event open clears it") {
    Port1 p;
    Bytes reset(12, 0);
    put(reset, 0, 0x20, 1);                                    // RecoveryMsg, no EndOfEvent
    put(reset, 4, 88, 8);
    p.feed(packet(68, 1, {message(55, summary()), message(11, reset)}));
    CHECK_FALSE(p.sink.event_open(68));                        // without the rule, mEI 0x20 would keep it open
    p.feed(packet(68, 2, {message(56, stats(0x80, 6))}));      // standalone: no marker
    const auto rs = p.records();
    REQUIRE(rs.size() == 2);
    CHECK(rs[1].kind == K::CLEAR_CHANNEL);
    CHECK(rs[1].flags == 0x02);
    CHECK(p.sink.counters().resets_with_event_open == 1);
}

TEST_CASE("closing: a duplicate packet produces nothing") {
    Port1 p;
    const auto open = packet(68, 5, {message(55, summary())});
    p.feed(open);
    p.feed(packet(68, 6, {message(56, stats(0x80, 5))}));      // closes: marker
    p.feed(open);                                              // duplicate: dropped, does not reopen
    p.feed(packet(68, 7, {message(56, stats(0x80, 6))}));      // standalone: nothing
    const auto rs = p.records();
    REQUIRE(rs.size() == 2);
    CHECK(rs[1].kind == K::END_OF_EVENT);
    CHECK(p.decoder.counters().rejected_packets == 1);
}

TEST_CASE("closing: a discarded tail produces nothing after the fault") {
    Port1 p;
    Bytes bad = packet(68, 1, {message(55, summary())});
    le(bad, 0, 2);                                             // messageLength 0: the rest is discarded
    const auto closer = message(56, stats(0x80, 5));
    bad.insert(bad.end(), closer.begin(), closer.end());
    p.feed(bad);
    CHECK(p.records().size() == 1);                            // the ExecutionSummary only
    CHECK(p.sink.event_open(68));                              // its closer was lost
    CHECK(p.walker.counters().malformed_packets == 1);
}

TEST_CASE("buffer capacity: never grown, overflow counted") {
    Port1 p(2);
    p.feed(packet(68, 1, {message(55, summary()), message(55, summary()), message(55, summary())}));
    CHECK(p.sink.size() == 2);
    CHECK(p.sink.counters().overflow == 1);
    CHECK(p.sink.counters().records == 2);
}

// ---- DESIGN.md's hex examples ----

TEST_CASE("DESIGN.md example: B3's sample packet") {
    const std::string hex = read_file(std::string(B3_REPO_ROOT) +
                                      "/feed_generator/tests/fixtures/b3_sample_packet/packet.hex");
    std::istringstream lines(hex);
    Bytes datagram;
    for (std::string line; std::getline(lines, line);) {
        if (line.empty() || line[0] == '#') continue;
        std::istringstream bytes(line);
        for (std::string byte; bytes >> byte;) datagram.push_back(static_cast<std::uint8_t>(std::stoul(byte, nullptr, 16)));
    }
    Port1 p;
    p.feed(datagram);
    CHECK(concat(p.records()) == design_example("sample_packet"));
}

TEST_CASE("DESIGN.md example: trade event closed by ExecutionStatistics") {
    // tools/tests/fixtures/trade_event_intent.jsonl: PETR4, channel 70, packet 16.
    const std::uint64_t t = 1792414800042076781ull, sid = 100000001;
    Bytes es(64, 0);
    put(es, 0, sid, 8); put(es, 10, 2, 1); put(es, 12, 385100, 8); put(es, 20, 100, 8); put(es, 44, t, 8);
    put(es, 52, 26, 4); put(es, 56, t, 8);
    Bytes tr(56, 0);
    put(tr, 0, sid, 8); put(tr, 9, 1, 1); put(tr, 10, 8196, 2); put(tr, 12, 385100, 8); put(tr, 20, 100, 8);
    put(tr, 28, 3, 4); put(tr, 32, 15, 4); put(tr, 36, 14, 4); put(tr, 40, 20745, 2); put(tr, 44, t, 8);
    put(tr, 52, 27, 4);
    Bytes ch(72, 0);
    put(ch, 0, sid, 8); put(ch, 9, 1, 1); put(ch, 10, '0', 1); put(ch, 12, 385100, 8); put(ch, 20, 200, 8);
    put(ch, 32, 15, 4); put(ch, 36, 1792414800030414200ull, 8); put(ch, 44, 1000000000007ull, 8);
    put(ch, 52, 28, 4); put(ch, 56, t, 8); put(ch, 64, 300, 8);
    Bytes st(52, 0);
    put(st, 0, sid, 8); put(st, 8, 0x80, 1); put(st, 9, 1, 1); put(st, 10, 20745, 2); put(st, 12, 600, 8);
    put(st, 20, 385117, 8); put(st, 28, 1000000, 8); put(st, 36, 3, 4); put(st, 40, t, 8); put(st, 48, 29, 4);
    Port1 p;
    p.feed(packet(70, 16, {message(55, es), message(53, tr), message(50, ch), message(56, st)}, 1,
                  1792414800042081781ull));
    CHECK(concat(p.records()) == design_example("trade_event"));
}

TEST_CASE("DESIGN.md example: events.bin header of the default build") {
    const auto h = b3::records::events_header();
    Bytes got(sizeof h);
    std::memcpy(got.data(), &h, sizeof h);
    Bytes want = design_example("events_header");
    if (b3::kPublishedDepth != 5 || b3::kTrackedDepth != 16 || b3::kMaxInstruments != 64 ||
        b3::kBookMode != b3::BookMode::Capped) {
        // A variant build: only N (16..17), K (18..19) and MAX_INSTRUMENTS (20..23) may differ.
        for (std::size_t i = 16; i < 24; ++i) want[i] = got[i];
    }
    CHECK(got == want);
}

namespace {
// The tracker first, then port 1, as b3_decode runs them.
struct TrackerThenEvents {
    b3::tracker::Tracker& t;
    b3::event_sink::EventSink& e;
    template <std::uint16_t Id>
    void on(const b3::walker::MessageView& m) {
        t.on<Id>(m);
        e.on<Id>(m);
    }
    void on_unknown(const b3::walker::MessageView& m) {
        t.on_unknown(m);
        e.on_unknown(m);
    }
};
}  // namespace

TEST_CASE("flag bit 3: a table instrument that is not CLEAN; channel-wide records never") {
    const auto table = b3::instruments::Table::parse("7 68\n");
    std::vector<EventRecord> buf(16);
    b3::packet::PacketDecoder<> decoder;
    b3::tracker::Tracker tracker(table, decoder.sequencer());
    b3::event_sink::EventSink events(buf.data(), buf.size(), &tracker);
    TrackerThenEvents sink{tracker, events};
    b3::walker::Walker walker;
    std::vector<Bytes> keep;
    auto feed = [&](Bytes d) {
        keep.push_back(std::move(d));
        const Bytes& p = keep.back();
        b3::packet::PacketHeader h;
        if (!decoder.decode(p.data(), p.size(), h)) return;
        const auto& v = decoder.sequencer().last();
        if (v.kind == b3::packet::SequenceVerdict::Kind::gap) tracker.on_loss({h.channel, v.gap_version, v.gap_first});
        walker.walk(h, p.data(), p.size(), sink);
    };
    Bytes empty(20, 0);
    put(empty, 0, 7, 8);
    Bytes reset(12, 0);
    put(reset, 0, 0x80, 1);
    feed(packet(68, 1, {message(50, order(0x80, 0, '0', 1, 1, 0, 1, 1, 1, kNull64))}));   // clean
    feed(packet(68, 3, {message(50, order(0x80, 0, '0', 1, 1, 0, 2, 2, 1, kNull64)),      // gap: stale
                        message(1, {})}));
    feed(packet(68, 4, {message(9, empty)}));                                               // EmptyBook: clean again
    feed(packet(68, 5, {message(50, order(0x80, 0, '0', 1, 1, 0, 3, 1, 1, kNull64)),
                        message(50, order(0x00, 0, '0', 1, 1, 0, 4, 5, 1, kNull64)),      // rptSeq jump: stale
                        message(11, reset)}));                                              // ChannelReset: clears
    feed(packet(68, 6, {message(50, order(0x80, 0, '0', 1, 1, 0, 5, 6, 1, kNull64))}));   // CLEAN after the ChannelReset
    const std::vector<EventRecord> rs(buf.begin(), buf.begin() + events.size());
    REQUIRE(rs.size() == 8);
    CHECK((rs[0].flags & 0x08) == 0);
    CHECK((rs[1].flags & 0x08) == 0x08);   // the packet revealing the gap is stale
    CHECK((rs[2].flags & 0x08) == 0);      // SEQ_RESET: no instrument
    CHECK((rs[3].flags & 0x08) == 0);      // CLEAR_BOOK: its own EmptyBook made it CLEAN
    CHECK((rs[4].flags & 0x08) == 0);
    CHECK((rs[5].flags & 0x08) == 0x08);   // the jump: stale from this message
    CHECK(rs[6].kind == K::CLEAR_CHANNEL);
    CHECK((rs[6].flags & 0x08) == 0);      // channel-wide
    CHECK((rs[7].flags & 0x08) == 0);      // after the ChannelReset
}
