// test_pcap.cpp: the pcap reader on pcap files built byte by byte in memory.
//
// What it does:
//   Builds small captures (global header, records, Ethernet/VLAN/IPv4/UDP
//   frames) with explicit byte writes, and checks the PacketViews and
//   counters the reader produces: both magics in both byte orders, rejected
//   formats, VLAN, IP options, padded short frames, non-IPv4, non-UDP,
//   fragments, bad lengths, truncated captures and records cut off at EOF.
//
// What it must not do:
//   Use the reader (or wire.hpp) to build its inputs.

#include <doctest.h>

#include <cstdint>
#include <string>
#include <vector>

#include "b3/pcap.hpp"

using b3::pcap::Capture;
using Bytes = std::vector<std::uint8_t>;

namespace {

void put(Bytes& b, std::uint64_t v, int n, bool big) {
    for (int i = 0; i < n; ++i) {
        const int shift = 8 * (big ? n - 1 - i : i);
        b.push_back(static_cast<std::uint8_t>(v >> shift));
    }
}

struct Frame {
    std::uint16_t ethertype = 0x0800;
    bool vlan = false;
    std::uint8_t version = 4;
    int ip_options = 0;                // 32-bit words of IPv4 options
    std::uint8_t protocol = 17;
    std::uint16_t flags_frag = 0x4000; // DF
    int total_len_delta = 0;           // added to the true IPv4 total length
    int udp_len_delta = 0;             // added to the true UDP length
    std::uint32_t src_ip = 0x0A000001; // 10.0.0.1
    std::uint32_t dst_ip = 0xE9FC0844; // 233.252.8.68
    std::uint16_t src_port = 50000;
    std::uint16_t dst_port = 30068;
    Bytes payload = {0xDE, 0xAD, 0xBE, 0xEF};
    std::size_t pad_to = 0;            // Ethernet padding: pad the frame to this length
};

Bytes frame(const Frame& f) {
    Bytes b = {0x01, 0x00, 0x5E, 0x7C, 0x08, 0x44, 0x02, 0xB3, 0, 0, 0, 1};
    if (f.vlan) {
        put(b, 0x8100, 2, true);
        put(b, 0x0064, 2, true);       // VLAN 100
    }
    put(b, f.ethertype, 2, true);
    if (f.ethertype != 0x0800) {
        b.insert(b.end(), 46, 0);
        return b;
    }
    const int ihl_words = 5 + f.ip_options;
    const int udp_len = 8 + static_cast<int>(f.payload.size());
    const int total = ihl_words * 4 + udp_len;
    b.push_back(static_cast<std::uint8_t>((f.version << 4) | ihl_words));
    b.push_back(0);
    put(b, static_cast<std::uint64_t>(total + f.total_len_delta), 2, true);
    put(b, 0x1234, 2, true);
    put(b, f.flags_frag, 2, true);
    b.push_back(64);
    b.push_back(f.protocol);
    put(b, 0, 2, true);                // checksum, not verified
    put(b, f.src_ip, 4, true);
    put(b, f.dst_ip, 4, true);
    for (int i = 0; i < f.ip_options * 4; ++i) b.push_back(0x01);  // NOP options
    put(b, f.src_port, 2, true);
    put(b, f.dst_port, 2, true);
    put(b, static_cast<std::uint64_t>(udp_len + f.udp_len_delta), 2, true);
    put(b, 0, 2, true);
    b.insert(b.end(), f.payload.begin(), f.payload.end());
    while (b.size() < f.pad_to) b.push_back(0);
    return b;
}

struct File {
    bool nano = true;
    bool big = false;
    std::uint32_t link_type = 1;
    Bytes bytes;

    File(bool nano_ = true, bool big_ = false, std::uint32_t link = 1)
        : nano(nano_), big(big_), link_type(link) {
        put(bytes, nano ? 0xA1B23C4D : 0xA1B2C3D4, 4, big);
        put(bytes, 2, 2, big);
        put(bytes, 4, 2, big);
        put(bytes, 0, 4, big);
        put(bytes, 0, 4, big);
        put(bytes, 65535, 4, big);
        put(bytes, link_type, 4, big);
    }
    File& record(const Bytes& fr, std::uint32_t sec = 1, std::uint32_t frac = 2,
                 std::uint32_t orig_extra = 0) {
        put(bytes, sec, 4, big);
        put(bytes, frac, 4, big);
        put(bytes, fr.size(), 4, big);
        put(bytes, fr.size() + orig_extra, 4, big);
        bytes.insert(bytes.end(), fr.begin(), fr.end());
        return *this;
    }
    Capture load() const { return Capture::from_bytes(bytes); }
};

bool payload_is(const b3::pcap::PacketView& v, const Bytes& want) {
    return v.payload_len == want.size() && Bytes(v.payload, v.payload + v.payload_len) == want;
}

}  // namespace

TEST_CASE("nanosecond and microsecond magic, both byte orders") {
    for (const bool big : {false, true}) {
        CAPTURE(big);
        const auto ns = File(true, big).record(frame({}), 7, 123456789).load();
        REQUIRE(ns.packets().size() == 1);
        CHECK(ns.info().nanosecond);
        CHECK(ns.info().swapped == big);
        CHECK(ns.packets()[0].timestamp_ns == 7'123'456'789ull);

        const auto us = File(false, big).record(frame({}), 7, 123456).load();
        REQUIRE(us.packets().size() == 1);
        CHECK_FALSE(us.info().nanosecond);
        CHECK(us.packets()[0].timestamp_ns == 7'123'456'000ull);
    }
}

TEST_CASE("one datagram: view fields, zero-copy") {
    const auto cap = File().record(frame({})).record(frame({})).load();
    REQUIRE(cap.packets().size() == 2);
    const auto& v = cap.packets()[1];
    CHECK(v.frame_number == 2);
    CHECK(payload_is(v, {0xDE, 0xAD, 0xBE, 0xEF}));
    CHECK(v.dst_ip == 0xE9FC0844u);
    CHECK(b3::pcap::ipv4_to_string(v.dst_ip) == "233.252.8.68");
    CHECK(v.dst_port == 30068);
    CHECK(v.src_ip == 0x0A000001u);
    CHECK(v.src_port == 50000);
    const auto* begin = cap.buffer().data();
    CHECK(v.payload > begin);
    CHECK(v.payload + v.payload_len <= begin + cap.buffer().size());
    CHECK(cap.counters().frames == 2);
    CHECK(cap.counters().udp_payloads == 2);
    CHECK(cap.counters().payload_bytes == 8);
}

TEST_CASE("rejected files") {
    Bytes pcapng = {0x0A, 0x0D, 0x0D, 0x0A, 0x1C, 0, 0, 0, 0x4D, 0x3C, 0x2B, 0x1A};
    pcapng.resize(28, 0);
    CHECK_THROWS_WITH_AS(Capture::from_bytes(pcapng), doctest::Contains("pcapng"), b3::pcap::Error);
    CHECK_THROWS_WITH_AS(File(true, false, 101).load(), doctest::Contains("link type 101"),
                         b3::pcap::Error);
    CHECK_THROWS_WITH_AS(Capture::from_bytes(Bytes(24, 0x55)), doctest::Contains("unknown magic"),
                         b3::pcap::Error);
    CHECK_THROWS_WITH_AS(Capture::from_bytes(Bytes(10, 0)), doctest::Contains("shorter"),
                         b3::pcap::Error);
    CHECK_THROWS_AS(Capture::from_file("/nonexistent/b3.pcap"), b3::pcap::Error);
}

TEST_CASE("empty capture") {
    const auto cap = File().load();
    CHECK(cap.packets().empty());
    CHECK(cap.counters().frames == 0);
    CHECK(cap.counters().trailing_garbage == 0);
}

TEST_CASE("802.1Q VLAN tag") {
    Frame f;
    f.vlan = true;
    const auto cap = File().record(frame(f)).load();
    REQUIRE(cap.packets().size() == 1);
    CHECK(payload_is(cap.packets()[0], {0xDE, 0xAD, 0xBE, 0xEF}));
    CHECK(cap.packets()[0].dst_port == 30068);
}

TEST_CASE("IPv4 options are honoured") {
    Frame f;
    f.ip_options = 3;
    const auto cap = File().record(frame(f)).load();
    REQUIRE(cap.packets().size() == 1);
    CHECK(payload_is(cap.packets()[0], {0xDE, 0xAD, 0xBE, 0xEF}));
    CHECK(cap.packets()[0].src_port == 50000);
}

TEST_CASE("padded short frame: payload length is the UDP length") {
    Frame f;
    f.payload = Bytes(10, 0x42);   // shorter than a B3 packet header, like the malformed set's
    f.pad_to = 60;
    const auto fr = frame(f);
    REQUIRE(fr.size() == 60);
    const auto cap = File().record(fr).load();
    REQUIRE(cap.packets().size() == 1);
    CHECK(payload_is(cap.packets()[0], Bytes(10, 0x42)));

    Frame empty;                   // an empty datagram is still handed over
    empty.payload = {};
    empty.pad_to = 60;
    const auto cap0 = File().record(frame(empty)).load();
    REQUIRE(cap0.packets().size() == 1);
    CHECK(cap0.packets()[0].payload_len == 0);
}

TEST_CASE("non-IPv4 and non-UDP are counted and skipped") {
    Frame arp;
    arp.ethertype = 0x0806;
    Frame v6;
    v6.version = 6;
    Frame tcp;
    tcp.protocol = 6;
    Frame vlan_arp;
    vlan_arp.vlan = true;
    vlan_arp.ethertype = 0x0806;
    const auto cap = File().record(frame(arp)).record(frame(v6)).record(frame(tcp))
                         .record(frame(vlan_arp)).record(frame({})).load();
    CHECK(cap.counters().frames == 5);
    CHECK(cap.counters().non_ipv4 == 3);
    CHECK(cap.counters().non_udp == 1);
    REQUIRE(cap.packets().size() == 1);
    CHECK(cap.packets()[0].frame_number == 5);
}

TEST_CASE("fragments are counted and skipped") {
    Frame more;
    more.flags_frag = 0x2000;      // MF
    Frame offset;
    offset.flags_frag = 0x0010;    // offset 16 x 8 bytes
    const auto cap = File().record(frame(more)).record(frame(offset)).record(frame({})).load();
    CHECK(cap.counters().fragments == 2);
    CHECK(cap.packets().size() == 1);
}

TEST_CASE("bad lengths are counted and skipped") {
    Frame ip_past_frame;
    ip_past_frame.total_len_delta = 1;
    Frame udp_past_ip;
    udp_past_ip.udp_len_delta = 1;
    Frame udp_too_small;
    udp_too_small.udp_len_delta = -static_cast<int>(udp_too_small.payload.size()) - 1;  // 7
    Bytes tiny = {1, 2, 3, 4, 5};  // shorter than an Ethernet header
    Bytes short_ip = frame({});
    short_ip.resize(14 + 10);      // Ethernet header, half an IPv4 header
    Bytes bad_ihl = frame({});
    bad_ihl[14] = 0x44;            // IHL 4 words < 20 bytes
    const auto cap = File().record(frame(ip_past_frame)).record(frame(udp_past_ip))
                         .record(frame(udp_too_small)).record(tiny).record(short_ip)
                         .record(bad_ihl).record(frame({})).load();
    CHECK(cap.counters().frames == 7);
    CHECK(cap.counters().bad_lengths == 6);
    CHECK(cap.packets().size() == 1);

    // A UDP length shorter than the IPv4 payload is trusted: the rest is padding.
    Frame shorter;
    shorter.udp_len_delta = -2;
    const auto cap2 = File().record(frame(shorter)).load();
    REQUIRE(cap2.packets().size() == 1);
    CHECK(payload_is(cap2.packets()[0], {0xDE, 0xAD}));
}

TEST_CASE("truncated capture is counted and skipped") {
    const auto cap = File().record(frame({}), 1, 2, 100).record(frame({})).load();
    CHECK(cap.counters().frames == 2);
    CHECK(cap.counters().truncated == 1);
    REQUIRE(cap.packets().size() == 1);
    CHECK(cap.packets()[0].frame_number == 2);
}

TEST_CASE("record cut off at end of file stops cleanly") {
    File body;
    body.record(frame({})).record(frame({}));
    body.bytes.resize(body.bytes.size() - 5);          // second record's body cut
    const auto a = body.load();
    CHECK(a.counters().frames == 1);
    CHECK(a.packets().size() == 1);
    CHECK(a.counters().trailing_garbage == 16 + frame({}).size() - 5);

    File header;
    header.record(frame({}));
    header.bytes.insert(header.bytes.end(), {1, 2, 3, 4, 5, 6, 7});  // 7 bytes of a record header
    const auto b = header.load();
    CHECK(b.counters().frames == 1);
    CHECK(b.counters().trailing_garbage == 7);

    File huge;                                          // incl_len far past EOF
    huge.record(frame({}));
    const std::size_t at = huge.bytes.size();
    huge.record(frame({}));
    huge.bytes[at + 8] = 0xFF;
    huge.bytes[at + 11] = 0x7F;
    const auto c = huge.load();
    CHECK(c.counters().frames == 1);
    CHECK(c.counters().trailing_garbage == huge.bytes.size() - at);
}
