// packet.hpp: the packet decoder. One UDP payload in, one packet header out.
//
// What it does:
//   Parses B3's 16-byte packet header (spec/LAYOUTS.md section 3) with the
//   offsets from layouts.hpp and the loads from wire.hpp. A datagram shorter
//   than the header is discarded and counted: it uses up no sequence number
//   (docs/DESIGN.md 3.6). A datagram over B3's 1400-byte limit is counted as
//   oversize and still decoded (DESIGN.md 3.6.1).
//   Sequencing is a template parameter: the Sequencer decides whether a
//   parsed packet is accepted. Step 6 puts the per-channel rules there; until
//   then AcceptAll accepts every packet.
//
// What it must not do:
//   Look past the 16-byte packet header. The messages belong to the walker.

#pragma once

#include <cstddef>
#include <cstdint>

#include "b3/layouts.hpp"
#include "b3/wire.hpp"

namespace b3::packet {

namespace ph = layout::packet_header;

struct PacketHeader {
    std::uint8_t channel = 0;
    std::uint8_t reserved = 0;
    std::uint16_t seq_version = 0;
    std::uint32_t seq_num = 0;
    std::uint64_t sending_time = 0;
};

struct PacketCounters {
    std::uint64_t datagrams = 0;         // payloads handed in
    std::uint64_t short_datagrams = 0;   // shorter than the packet header: discarded
    std::uint64_t packets = 0;           // headers parsed
    std::uint64_t oversize_packets = 0;  // over max_packet_size bytes (still decoded)
    std::uint64_t rejected_packets = 0;  // parsed, not accepted by the sequencer (step 6)
};

// Reads the packet header. p must hold at least packet_header::size bytes.
inline PacketHeader read_header(const std::uint8_t* p) noexcept {
    PacketHeader h;
    h.channel = wire::load_le<ph::channelNumber::type>(p + ph::channelNumber::offset);
    h.reserved = wire::load_le<ph::reserved::type>(p + ph::reserved::offset);
    h.seq_version = wire::load_le<ph::sequenceVersion::type>(p + ph::sequenceVersion::offset);
    h.seq_num = wire::load_le<ph::sequenceNumber::type>(p + ph::sequenceNumber::offset);
    h.sending_time = wire::load_le<ph::sendingTime::type>(p + ph::sendingTime::offset);
    return h;
}

// Step 4: every parsed packet is accepted. Step 6 replaces this with the
// per-channel sequencing of the brief's section 6 (gaps, duplicates,
// heartbeats, SequenceReset).
struct AcceptAll {
    constexpr bool accept(const PacketHeader&) const noexcept { return true; }
};

template <class Sequencer = AcceptAll>
class PacketDecoder {
public:
    explicit PacketDecoder(Sequencer seq = Sequencer{}) : seq_(seq) {}

    // Parses one UDP payload. Returns true and fills out when the packet goes
    // on to the walker; false when it is discarded (short) or not accepted.
    bool decode(const std::uint8_t* payload, std::size_t len, PacketHeader& out) noexcept {
        ++counters_.datagrams;
        if (len < ph::size) {
            ++counters_.short_datagrams;
            return false;
        }
        ++counters_.packets;
        if (len > layout::max_packet_size) {
            ++counters_.oversize_packets;
        }
        out = read_header(payload);
        if (!seq_.accept(out)) {
            ++counters_.rejected_packets;
            return false;
        }
        return true;
    }

    const PacketCounters& counters() const noexcept { return counters_; }
    Sequencer& sequencer() noexcept { return seq_; }

private:
    Sequencer seq_;
    PacketCounters counters_;
};

}  // namespace b3::packet
