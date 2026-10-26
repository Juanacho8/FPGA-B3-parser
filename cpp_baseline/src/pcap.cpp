// pcap.cpp: the pcap reader (see include/b3/pcap.hpp).
//
// What it does:
//   Loads a pcap file whole, checks its global header, then walks the
//   records once: record header, Ethernet (one optional VLAN tag), IPv4,
//   UDP. Each UDP datagram becomes a PacketView into the buffer. Every
//   frame that yields none is counted in exactly one counter, checked in
//   this order: truncated capture, Ethernet too short, not IPv4, IPv4
//   header lengths, not UDP, fragment, UDP lengths. udp_shorter_than_ip
//   counts yielded datagrams whose UDP length leaves IPv4 payload unused.
//
// What it must not do:
//   Read the UDP payload, or stop on a damaged record: it counts, skips and
//   carries on, and a record cut off at end of file ends the walk cleanly.

#include "b3/pcap.hpp"

#include <cstdio>
#include <fstream>

#include "b3/wire.hpp"

namespace b3::pcap {

namespace {

// ---- File format constants ----
constexpr std::size_t kGlobalHeaderSize = 24;
constexpr std::size_t kRecordHeaderSize = 16;
constexpr std::uint32_t kMagicMicro = 0xA1B2C3D4;
constexpr std::uint32_t kMagicNano = 0xA1B23C4D;
constexpr std::uint32_t kMagicMicroSwapped = 0xD4C3B2A1;
constexpr std::uint32_t kMagicNanoSwapped = 0x4D3CB2A1;
constexpr std::uint32_t kPcapngSectionHeader = 0x0A0D0D0A;
constexpr std::uint32_t kLinkTypeEthernet = 1;

// ---- Network header constants (all big-endian on the wire) ----
constexpr std::size_t kEthHeaderSize = 14;
constexpr std::size_t kVlanTagSize = 4;
constexpr std::uint16_t kEtherTypeIpv4 = 0x0800;
constexpr std::uint16_t kEtherTypeVlan = 0x8100;
constexpr std::size_t kIpv4MinHeaderSize = 20;
constexpr std::uint8_t kIpProtoUdp = 17;
constexpr std::uint16_t kIpFlagMoreFragments = 0x2000;
constexpr std::uint16_t kIpFragmentOffsetMask = 0x1FFF;
constexpr std::size_t kUdpHeaderSize = 8;

constexpr std::uint64_t kNsPerSecond = 1'000'000'000;
constexpr std::uint64_t kNsPerMicrosecond = 1'000;

// Reads a pcap header field in the file's byte order.
template <class T>
T load_file(const std::uint8_t* p, bool swapped) {
    return swapped ? wire::load_be<T>(p) : wire::load_le<T>(p);
}

}  // namespace

Capture Capture::from_file(const std::filesystem::path& path) {
    std::error_code ec;
    const auto size = std::filesystem::file_size(path, ec);
    if (ec) {
        throw Error(path.string() + ": " + ec.message());
    }
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        throw Error(path.string() + ": cannot open");
    }
    std::vector<std::uint8_t> bytes(static_cast<std::size_t>(size));
    in.read(reinterpret_cast<char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
    if (static_cast<std::uintmax_t>(in.gcount()) != size) {
        throw Error(path.string() + ": short read");
    }
    return Capture(std::move(bytes));
}

Capture Capture::from_bytes(std::vector<std::uint8_t> bytes) {
    return Capture(std::move(bytes));
}

Capture::Capture(std::vector<std::uint8_t> bytes) : buffer_(std::move(bytes)) {
    index();
}

void Capture::index() {
    const std::uint8_t* const data = buffer_.data();
    const std::size_t size = buffer_.size();

    // ---- 1. Global header ----
    if (size < kGlobalHeaderSize) {
        throw Error("not a pcap file: " + std::to_string(size) +
                    " bytes, shorter than the 24-byte global header");
    }
    const std::uint32_t magic = wire::load_le<std::uint32_t>(data);
    switch (magic) {
        case kMagicNano: info_.nanosecond = true; info_.swapped = false; break;
        case kMagicMicro: info_.nanosecond = false; info_.swapped = false; break;
        case kMagicNanoSwapped: info_.nanosecond = true; info_.swapped = true; break;
        case kMagicMicroSwapped: info_.nanosecond = false; info_.swapped = true; break;
        case kPcapngSectionHeader:
            throw Error("pcapng is not supported, only classic pcap "
                        "(convert with: editcap -F pcap in.pcapng out.pcap)");
        default: {
            char hex[11];
            std::snprintf(hex, sizeof hex, "0x%08x", magic);
            throw Error(std::string("not a pcap file: unknown magic ") + hex);
        }
    }
    const bool sw = info_.swapped;
    info_.version_major = load_file<std::uint16_t>(data + 4, sw);
    info_.version_minor = load_file<std::uint16_t>(data + 6, sw);
    info_.snaplen = load_file<std::uint32_t>(data + 16, sw);
    info_.link_type = load_file<std::uint32_t>(data + 20, sw);
    if (info_.link_type != kLinkTypeEthernet) {
        throw Error("unsupported link type " + std::to_string(info_.link_type) +
                    ", only Ethernet (1)");
    }
    const std::uint64_t frac_to_ns = info_.nanosecond ? 1 : kNsPerMicrosecond;

    // ---- 2. Records ----
    std::size_t pos = kGlobalHeaderSize;
    std::uint64_t frame_number = 0;
    while (pos < size) {
        if (size - pos < kRecordHeaderSize) {
            counters_.trailing_garbage = size - pos;   // record header cut off
            break;
        }
        const std::uint8_t* rec = data + pos;
        const std::uint32_t ts_sec = load_file<std::uint32_t>(rec + 0, sw);
        const std::uint32_t ts_frac = load_file<std::uint32_t>(rec + 4, sw);
        const std::uint32_t incl_len = load_file<std::uint32_t>(rec + 8, sw);
        const std::uint32_t orig_len = load_file<std::uint32_t>(rec + 12, sw);
        if (incl_len > size - pos - kRecordHeaderSize) {
            counters_.trailing_garbage = size - pos;   // record body cut off
            break;
        }
        const std::uint8_t* frame = rec + kRecordHeaderSize;
        const std::size_t cap = incl_len;
        pos += kRecordHeaderSize + cap;
        ++frame_number;
        ++counters_.frames;

        if (incl_len < orig_len) {
            ++counters_.truncated;
            continue;
        }

        // ---- 3. Ethernet, one optional 802.1Q tag ----
        if (cap < kEthHeaderSize) {
            ++counters_.bad_lengths;
            continue;
        }
        std::size_t l3 = kEthHeaderSize;
        std::uint16_t ethertype = wire::load_be<std::uint16_t>(frame + 12);
        if (ethertype == kEtherTypeVlan) {
            if (cap < kEthHeaderSize + kVlanTagSize) {
                ++counters_.bad_lengths;
                continue;
            }
            ethertype = wire::load_be<std::uint16_t>(frame + 16);
            l3 += kVlanTagSize;
        }
        if (ethertype != kEtherTypeIpv4) {
            ++counters_.non_ipv4;
            continue;
        }

        // ---- 4. IPv4 ----
        if (cap - l3 < kIpv4MinHeaderSize) {
            ++counters_.bad_lengths;
            continue;
        }
        const std::uint8_t* ip = frame + l3;
        if ((ip[0] >> 4) != 4) {
            ++counters_.non_ipv4;
            continue;
        }
        const std::size_t ihl = static_cast<std::size_t>(ip[0] & 0x0F) * 4;
        const std::size_t total_len = wire::load_be<std::uint16_t>(ip + 2);
        if (ihl < kIpv4MinHeaderSize || total_len < ihl || total_len > cap - l3) {
            ++counters_.bad_lengths;   // header length, or total length past the captured frame
            continue;
        }
        // Header checksum: not verified. To add it, sum the ihl bytes at ip
        // as 16-bit big-endian words (ones' complement); the result must be 0xFFFF.
        if (ip[9] != kIpProtoUdp) {
            ++counters_.non_udp;
            continue;
        }
        const std::uint16_t flags_frag = wire::load_be<std::uint16_t>(ip + 6);
        if ((flags_frag & kIpFlagMoreFragments) != 0 || (flags_frag & kIpFragmentOffsetMask) != 0) {
            ++counters_.fragments;
            continue;
        }

        // ---- 5. UDP: the payload length comes from the UDP length field ----
        if (total_len - ihl < kUdpHeaderSize) {
            ++counters_.bad_lengths;
            continue;
        }
        const std::uint8_t* udp = ip + ihl;
        const std::size_t udp_len = wire::load_be<std::uint16_t>(udp + 4);
        if (udp_len < kUdpHeaderSize || udp_len > total_len - ihl) {
            ++counters_.bad_lengths;
            continue;
        }
        // UDP checksum: not verified either (same place, over the pseudo-header).
        if (udp_len < total_len - ihl) {
            ++counters_.udp_shorter_than_ip;   // still yielded, at the UDP length
        }

        PacketView v{};
        v.frame_number = frame_number;
        v.timestamp_ns = static_cast<std::uint64_t>(ts_sec) * kNsPerSecond + ts_frac * frac_to_ns;
        v.payload = udp + kUdpHeaderSize;
        v.payload_len = static_cast<std::uint32_t>(udp_len - kUdpHeaderSize);
        v.src_ip = wire::load_be<std::uint32_t>(ip + 12);
        v.dst_ip = wire::load_be<std::uint32_t>(ip + 16);
        v.src_port = wire::load_be<std::uint16_t>(udp + 0);
        v.dst_port = wire::load_be<std::uint16_t>(udp + 2);
        packets_.push_back(v);
        ++counters_.udp_payloads;
        counters_.payload_bytes += v.payload_len;
    }
}

std::string ipv4_to_string(std::uint32_t addr) {
    return std::to_string(addr >> 24) + "." + std::to_string((addr >> 16) & 0xFF) + "." +
           std::to_string((addr >> 8) & 0xFF) + "." + std::to_string(addr & 0xFF);
}

}  // namespace b3::pcap
