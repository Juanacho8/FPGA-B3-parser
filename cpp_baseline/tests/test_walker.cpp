// test_walker.cpp: packet decoder and message walker on hand-built payloads.
//
// What it does:
//   Builds B3 packets byte by byte (16-byte packet header, 12-byte message
//   headers, bodies) and checks what reaches the sink and the counters:
//   one message, back to back messages, a full 1400-byte packet, oversize,
//   every malformed reason, a fault in the second message, unknown
//   templates, short datagrams, header-only packets and a blockLength
//   larger than the schema's.
//
// What it must not do:
//   Build inputs with the code under test. Every offset here is a literal
//   from spec/LAYOUTS.md sections 3 and 4.

#include <doctest.h>

#include <cstdint>
#include <utility>
#include <vector>

#include "b3/packet.hpp"
#include "b3/walker.hpp"

using Bytes = std::vector<std::uint8_t>;
using b3::walker::Malformed;
using b3::walker::MessageView;

namespace {

void le(Bytes& b, std::uint64_t v, int n) {
    for (int i = 0; i < n; ++i) b.push_back(static_cast<std::uint8_t>(v >> (8 * i)));
}

Bytes packet_header(std::uint8_t channel = 68, std::uint16_t version = 3, std::uint32_t seq = 77,
                    std::uint64_t sending = 0x1122334455667788ull) {
    Bytes b;
    b.push_back(channel);
    b.push_back(0);
    le(b, version, 2);
    le(b, seq, 4);
    le(b, sending, 8);
    return b;
}

struct Msg {
    std::uint16_t template_id = 50;
    std::uint16_t block_length = 72;
    std::size_t body = 72;               // body bytes actually written
    std::int32_t length_override = -1;   // messageLength field, if not 12 + body
    std::uint16_t encoding = 0xEB50;
    std::uint16_t schema = 2;
    std::uint16_t version = 17;
    std::uint8_t fill = 0xAB;
};

void add(Bytes& b, const Msg& m) {
    const auto length = m.length_override >= 0 ? static_cast<std::uint64_t>(m.length_override)
                                               : 12 + m.body;
    le(b, length, 2);
    le(b, m.encoding, 2);
    le(b, m.block_length, 2);
    le(b, m.template_id, 2);
    le(b, m.schema, 2);
    le(b, m.version, 2);
    b.insert(b.end(), m.body, m.fill);
}

Bytes packet(std::initializer_list<Msg> msgs) {
    Bytes b = packet_header();
    for (const auto& m : msgs) add(b, m);
    return b;
}

struct Recorder {
    std::vector<MessageView> seen;
    std::vector<bool> unknown;
    template <std::uint16_t TemplateId>
    void on(const MessageView& m) {
        CHECK(m.template_id == TemplateId);
        seen.push_back(m);
        unknown.push_back(false);
    }
    void on_unknown(const MessageView& m) {
        seen.push_back(m);
        unknown.push_back(true);
    }
};

// Owns its datagram, so the views in sink.seen stay valid for the test.
struct Run {
    Bytes datagram;
    Recorder sink;
    b3::packet::PacketDecoder<> decoder;
    b3::walker::Walker walker;
    bool decoded = false;

    explicit Run(Bytes d) : datagram(std::move(d)) {
        b3::packet::PacketHeader h;
        decoded = decoder.decode(datagram.data(), datagram.size(), h);
        if (decoded) walker.walk(h, datagram.data(), datagram.size(), sink);
    }
    std::uint64_t malformed(Malformed r) const {
        return walker.counters().malformed[static_cast<std::size_t>(r)];
    }
};

}  // namespace

TEST_CASE("packet header fields") {
    const Bytes d = packet_header(70, 0xBEEF, 0xA1B2C3D4u, 0x0102030405060708ull);
    b3::packet::PacketDecoder<> dec;
    b3::packet::PacketHeader h;
    REQUIRE(dec.decode(d.data(), d.size(), h));
    CHECK(h.channel == 70);
    CHECK(h.seq_version == 0xBEEF);
    CHECK(h.seq_num == 0xA1B2C3D4u);
    CHECK(h.sending_time == 0x0102030405060708ull);
    CHECK(dec.counters().packets == 1);
}

TEST_CASE("one message") {
    Run r(packet({Msg{}}));
    REQUIRE(r.sink.seen.size() == 1);
    const auto& m = r.sink.seen[0];
    CHECK(m.msg_index == 0);
    CHECK(m.template_id == 50);
    CHECK(m.block_length == 72);
    CHECK(m.message_length == 84);
    CHECK(m.version == 17);
    CHECK(m.packet.seq_num == 77);
    CHECK(m.body == m.message + 12);
    CHECK(m.body[0] == 0xAB);
    CHECK(r.walker.counters().messages == 1);
    CHECK(r.walker.counters().malformed_packets == 0);
}

TEST_CASE("several messages back to back, no padding") {
    Run r(packet({Msg{}, Msg{53, 56, 56}, Msg{2, 4, 4}}));
    const Bytes& d = r.datagram;
    REQUIRE(r.sink.seen.size() == 3);
    CHECK(r.sink.seen[0].message == d.data() + 16);
    CHECK(r.sink.seen[1].message == d.data() + 16 + 84);
    CHECK(r.sink.seen[2].message == d.data() + 16 + 84 + 68);
    CHECK(r.sink.seen[1].template_id == 53);
    CHECK(r.sink.seen[2].msg_index == 2);
    CHECK(r.walker.counters().malformed_packets == 0);
}

TEST_CASE("full 1400-byte packet, and one byte over") {
    // 16 + 16 x 84 = 1360, plus a 40-byte message: 1400 exactly.
    Bytes d = packet_header();
    for (int i = 0; i < 16; ++i) add(d, Msg{});
    add(d, Msg{21, 28, 28});
    REQUIRE(d.size() == 1400);
    Run r(d);
    CHECK(r.sink.seen.size() == 17);
    CHECK(r.decoder.counters().oversize_packets == 0);
    CHECK(r.walker.counters().malformed_packets == 0);

    add(d, Msg{2, 0, 0, -1});            // a 12-byte message: 1412 bytes
    Run over(d);
    CHECK(over.decoder.counters().oversize_packets == 1);
    CHECK(over.sink.seen.size() == 18);  // counted, still walked (DESIGN.md 3.6.1)
}

TEST_CASE("malformed reasons, each in the first message") {
    struct Case {
        Msg msg;
        Malformed reason;
    };
    const Case cases[] = {
        {Msg{50, 72, 72, 0}, Malformed::length_zero},
        {Msg{50, 72, 72, 8}, Malformed::length_too_small},
        {Msg{50, 72, 72, 1000}, Malformed::length_past_end},   // past the whole datagram
        {Msg{50, 72, 72, -1, 0xEB51}, Malformed::bad_encoding},
        {Msg{50, 72, 72, -1, 0xEB50, 3}, Malformed::bad_schema_id},
        {Msg{50, 73, 72}, Malformed::block_length_past_end},
    };
    for (const auto& c : cases) {
        CAPTURE(static_cast<int>(c.reason));
        Run r(packet({c.msg, Msg{}}));
        CHECK(r.sink.seen.empty());
        CHECK(r.walker.counters().malformed_packets == 1);
        CHECK(r.malformed(c.reason) == 1);
    }
    // Header truncated: fewer than 12 bytes after the last complete message.
    Bytes d = packet({Msg{}});
    d.insert(d.end(), {0x54, 0x00, 0x50, 0xEB, 0x48});   // 5 bytes of a header
    Run r(d);
    CHECK(r.sink.seen.size() == 1);
    CHECK(r.malformed(Malformed::header_truncated) == 1);
}

TEST_CASE("test order: the first failing test names the reason") {
    // messageLength 8 and a bad encoding: length is tested first.
    Run a(packet({Msg{50, 72, 72, 8, 0xEB51}}));
    CHECK(a.malformed(Malformed::length_too_small) == 1);
    // Bad encoding and bad schemaId: encoding first.
    Run b(packet({Msg{50, 72, 72, -1, 0xEB51, 3}}));
    CHECK(b.malformed(Malformed::bad_encoding) == 1);
    // Bad schemaId and blockLength too large: schemaId first.
    Run c(packet({Msg{50, 99, 72, -1, 0xEB50, 3}}));
    CHECK(c.malformed(Malformed::bad_schema_id) == 1);
}

TEST_CASE("fault in the second message: the first is still dispatched") {
    Run r(packet({Msg{}, Msg{51, 52, 52, -1, 0x1234}, Msg{}}));
    REQUIRE(r.sink.seen.size() == 1);
    CHECK(r.sink.seen[0].msg_index == 0);
    CHECK(r.malformed(Malformed::bad_encoding) == 1);
    CHECK(r.walker.counters().messages == 1);
}

TEST_CASE("unknown template is skipped by messageLength and counted") {
    Run r(packet({Msg{999, 20, 20, -1, 0xEB50, 2, 18}, Msg{}}));
    REQUIRE(r.sink.seen.size() == 2);
    CHECK(r.sink.unknown[0]);
    CHECK(r.sink.seen[0].template_id == 999);
    CHECK_FALSE(r.sink.unknown[1]);
    CHECK(r.sink.seen[1].template_id == 50);
    CHECK(r.sink.seen[1].msg_index == 1);
    CHECK(r.walker.counters().unknown_templates == 1);
    CHECK(r.walker.counters().malformed_packets == 0);
}

TEST_CASE("blockLength larger than the schema's is skipped correctly") {
    // Order_MBO at version 18 with 8 extra root-block bytes, then a Trade.
    Run r(packet({Msg{50, 80, 80, -1, 0xEB50, 2, 18}, Msg{53, 56, 56}}));
    const Bytes& d = r.datagram;
    REQUIRE(r.sink.seen.size() == 2);
    CHECK(r.sink.seen[0].block_length == 80);
    CHECK(r.sink.seen[0].message_length == 92);
    CHECK(r.sink.seen[1].message == d.data() + 16 + 92);
    CHECK(r.sink.seen[1].template_id == 53);
}

TEST_CASE("groups and var data are covered by messageLength") {
    // A News with 36-byte root block and 20 bytes of var data after it.
    Run r(packet({Msg{5, 36, 56}, Msg{}}));
    REQUIRE(r.sink.seen.size() == 2);
    CHECK(r.sink.seen[0].message_length == 68);
    CHECK(r.sink.seen[1].template_id == 50);
}

TEST_CASE("short datagram is discarded and counted") {
    for (std::size_t n : {0u, 1u, 10u, 15u}) {
        CAPTURE(n);
        Run r(Bytes(n, 0x42));
        CHECK_FALSE(r.decoded);
        CHECK(r.decoder.counters().short_datagrams == 1);
        CHECK(r.decoder.counters().packets == 0);
        CHECK(r.sink.seen.empty());
    }
}

TEST_CASE("header-only packet is empty, not malformed") {
    Run r(packet_header());
    CHECK(r.decoded);
    CHECK(r.walker.counters().empty_packets == 1);
    CHECK(r.walker.counters().malformed_packets == 0);
    CHECK(r.sink.seen.empty());
}

TEST_CASE("every schema template has a dispatch case") {
    for (const auto id : b3::layout::template_ids) {
        CAPTURE(id);
        CHECK(b3::walker::is_known_template(id));
        Recorder sink;
        const MessageView m{{}, 0, id, 17, 0, 12, nullptr, nullptr};
        CHECK(b3::walker::dispatch(m, sink));
        CHECK_FALSE(sink.unknown.at(0));
    }
    for (const std::uint16_t id : {0, 4, 6, 49, 58, 70, 72, 999, 65535}) {
        CAPTURE(id);
        CHECK_FALSE(b3::walker::is_known_template(id));
        Recorder sink;
        CHECK_FALSE(b3::walker::dispatch(MessageView{{}, 0, id, 17, 0, 12, nullptr, nullptr}, sink));
    }
}

TEST_CASE("CountingSink: 256 slots, larger ids only in the unknown counter") {
    b3::walker::CountingSink sink;
    static_assert(sizeof(sink.per_template) / sizeof(sink.per_template[0]) == 256);
    b3::walker::Walker walker;
    const Bytes d = packet({Msg{999, 20, 20, -1, 0xEB50, 2, 18}, Msg{}, Msg{4, 0, 0}});
    b3::packet::PacketDecoder<> dec;
    b3::packet::PacketHeader h;
    REQUIRE(dec.decode(d.data(), d.size(), h));
    walker.walk(h, d.data(), d.size(), sink);

    // 999, as in the generator's schema set: unknown, in no slot.
    CHECK(sink.unknown == 2);                       // 999 and 4
    CHECK(walker.counters().unknown_templates == 2);
    // A known id is counted in its own slot.
    CHECK(sink.per_template[50] == 1);
    // An unknown id below 256 keeps a slot too.
    CHECK(sink.per_template[4] == 1);
    std::uint64_t total = 0;
    for (const auto n : sink.per_template) total += n;
    CHECK(total == 2);                              // 50 and 4; 999 has no slot
}

TEST_CASE("msg_index overflow: more than 256 messages, nothing after index 255") {
    // 300 SequenceReset messages (12 bytes each): an oversize packet no real
    // feed can send (DESIGN.md 3.6.1), walked to exercise the guard.
    Bytes d = packet_header();
    for (int i = 0; i < 300; ++i) add(d, Msg{1, 0, 0});
    Run r(std::move(d));
    REQUIRE(r.sink.seen.size() == 256);
    CHECK(r.sink.seen.back().msg_index == 255);
    CHECK(r.walker.counters().msg_index_overflow == 1);
    CHECK(r.walker.counters().malformed_packets == 0);
    CHECK(r.walker.counters().messages == 256);

    // Exactly 256 messages fit: indices 0..255, no overflow.
    Bytes ok = packet_header();
    for (int i = 0; i < 256; ++i) add(ok, Msg{1, 0, 0});
    Run fits(std::move(ok));
    CHECK(fits.sink.seen.size() == 256);
    CHECK(fits.walker.counters().msg_index_overflow == 0);
}
