// pcap.hpp: the pcap reader. Whole file in memory, then an index of UDP payloads.
//
// What it does:
//   Capture::from_file reads a classic pcap file into one buffer, then walks
//   it once and builds a vector of PacketViews, one per UDP datagram. Both
//   happen at startup, before any timed code. A PacketView points into the
//   buffer (zero-copy) and carries the capture timestamp in ns and the IPv4
//   destination (multicast group) and source. Every frame that yields no
//   view lands in exactly one counter.
//
//   Accepted: classic pcap, nanosecond magic 0xa1b23c4d or microsecond magic
//   0xa1b2c3d4, either byte order; link type 1 (Ethernet); one optional
//   802.1Q VLAN tag; IPv4 with options; UDP. Rejected with pcap::Error:
//   pcapng, any other magic, any other link type.
//
// What it must not do:
//   Look at a single byte of the UDP payload, map multicast groups to
//   channels (configuration, later), or drop short payloads. Deciding what
//   is malformed is the packet decoder's job (step 4). A payload's length is
//   the UDP length field's, never the frame's (Ethernet pads short frames).

#pragma once

#include <cstdint>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <vector>

namespace b3::pcap {

// One UDP datagram, inside Capture's buffer.
struct PacketView {
    std::uint64_t frame_number;    // 1-based record number in the file (as Wireshark shows it)
    std::uint64_t timestamp_ns;    // capture time, ns since epoch
    const std::uint8_t* payload;   // UDP payload, inside the buffer
    std::uint32_t payload_len;     // from the UDP length field
    std::uint32_t dst_ip;          // IPv4 destination, host order (a.b.c.d = a << 24 | ...)
    std::uint32_t src_ip;          // IPv4 source, for debugging
    std::uint16_t dst_port;        // UDP destination port
    std::uint16_t src_port;        // UDP source port, for debugging
};
static_assert(sizeof(PacketView) == 40);

struct Counters {
    std::uint64_t frames = 0;             // records read whole (header and body inside the file)
    std::uint64_t udp_payloads = 0;       // PacketViews yielded
    std::uint64_t payload_bytes = 0;      // sum of their payload_len
    std::uint64_t non_ipv4 = 0;           // ethertype not IPv4 (after one VLAN tag), or IP version not 4
    std::uint64_t non_udp = 0;            // IPv4, protocol not 17
    std::uint64_t fragments = 0;          // IPv4 UDP with MF set or a nonzero fragment offset
    std::uint64_t truncated = 0;          // captured length < original length
    std::uint64_t bad_lengths = 0;        // a header or length field that does not fit
    std::uint64_t trailing_garbage = 0;   // bytes after the last whole record (record cut off at EOF)
    // Not a skip: datagrams yielded whose UDP length is shorter than the IPv4
    // payload. The UDP length is trusted and the rest treated as padding.
    std::uint64_t udp_shorter_than_ip = 0;
};

struct FileInfo {
    bool nanosecond = true;       // false: microsecond timestamps, converted to ns
    bool swapped = false;         // file written in the other byte order
    std::uint16_t version_major = 0;
    std::uint16_t version_minor = 0;
    std::uint32_t snaplen = 0;
    std::uint32_t link_type = 0;
};

class Error : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class Capture {
public:
    // Reads the whole file, then indexes it. Throws pcap::Error.
    static Capture from_file(const std::filesystem::path& path);
    // Takes ownership of the bytes of a pcap file, then indexes them. Throws pcap::Error.
    static Capture from_bytes(std::vector<std::uint8_t> bytes);

    // PacketViews point into the buffer: moving keeps it, copying would not.
    Capture(Capture&&) noexcept = default;
    Capture& operator=(Capture&&) noexcept = default;
    Capture(const Capture&) = delete;
    Capture& operator=(const Capture&) = delete;

    const std::vector<PacketView>& packets() const noexcept { return packets_; }
    const Counters& counters() const noexcept { return counters_; }
    const FileInfo& info() const noexcept { return info_; }
    const std::vector<std::uint8_t>& buffer() const noexcept { return buffer_; }

private:
    explicit Capture(std::vector<std::uint8_t> bytes);
    void index();

    std::vector<std::uint8_t> buffer_;
    std::vector<PacketView> packets_;
    Counters counters_;
    FileInfo info_;
};

// "a.b.c.d" for an address in host order.
std::string ipv4_to_string(std::uint32_t addr);

}  // namespace b3::pcap
