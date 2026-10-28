// packet.hpp: the packet decoder. One UDP payload in, one packet header out.
//
// What it does:
//   Parses B3's 16-byte packet header (spec/LAYOUTS.md section 3) with the
//   offsets from layouts.hpp and the loads from wire.hpp. A datagram shorter
//   than the header is discarded and counted: it uses up no sequence number
//   (docs/DESIGN.md 3.6). A datagram over B3's 1400-byte limit is counted as
//   oversize and still decoded (DESIGN.md 3.6.1).
//   Sequencing is a template parameter, Sequencer by default: per channel it
//   accepts the next packet, counts gaps, drops duplicates, checks heartbeat
//   nextSeqNo and handles SequenceReset (DESIGN.md 3.6.3, spec/LAYOUTS.md
//   12.1, feed_generator/tests/book_builder.py).
//
// What it must not do:
//   Look past the 16-byte packet header, except for the sequencer reading the
//   first message's header (is it a SequenceReset?). Message contents belong
//   to the walker; a heartbeat's nextSeqNo is checked when it is dispatched.
//   Allocate: the sequencer's state is a fixed array of 256 channels.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

#include "b3/framing.hpp"
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
    std::uint64_t rejected_packets = 0;  // parsed, not accepted by the sequencer (duplicates)
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

// ---- Sequencing (DESIGN.md 3.6.3) ----

// The templateId of the packet's first message when it is complete
// (framing.hpp's test), else kNoFirstMessage. The only look past the packet
// header: one message header.
inline constexpr std::uint32_t kNoFirstMessage = 0x10000;

inline std::uint32_t first_template(const std::uint8_t* payload, std::size_t len) noexcept {
    framing::MessageHeader h;
    if (len <= ph::size || framing::check(payload + ph::size, len - ph::size, h)) return kNoFirstMessage;
    return h.template_id;
}

struct SequencerCounters {
    std::uint64_t sequenced = 0;            // packets with sequenceNumber != 0, reset packets included
    std::uint64_t gaps = 0;                 // packets missing (sum of the jumps)
    std::uint64_t gap_events = 0;           // jumps
    std::uint64_t duplicates = 0;           // dropped: sequenceNumber at or below the last accepted
    std::uint64_t late_after_gap = 0;       // dropped: inside a gap this channel recorded (not a duplicate)
    std::uint64_t heartbeats = 0;           // sequenceNumber 0
    std::uint64_t heartbeat_mismatches = 0; // a heartbeat's nextSeqNo != the next expected number
    std::uint64_t heartbeat_without_sequence = 0;  // a heartbeat whose first message is not a Sequence
    std::uint64_t resets = 0;               // packets whose first message is a SequenceReset
    std::uint64_t reset_mismatches = 0;     // packet after a reset packet not (version + 1, 1)
    std::uint64_t version_changes = 0;      // sequenceVersion changed without a reset packet
};

// What the last packet did to its channel's sequence.
struct SequenceVerdict {
    enum class Kind : std::uint8_t { next, gap, duplicate, late, heartbeat, reset, first, version_change };
    Kind kind = Kind::first;
    std::uint16_t gap_version = 0;   // for a gap: the first missing (version, sequenceNumber)
    std::uint32_t gap_first = 0;
    std::uint32_t gap_count = 0;
};

class Sequencer {
public:
    static constexpr std::uint16_t kMaxVersion = 65534;   // then back to 1 (LAYOUTS.md 3)
    // Gaps remembered per channel to tell a late packet from a duplicate. A
    // late packet for an older gap than these is counted as a duplicate.
    static constexpr std::size_t kRecordedGaps = 8;

    // Decides one packet from its header and its first message's header.
    // True if it goes on to the walker; false for a duplicate.
    bool accept(const PacketHeader& h, const std::uint8_t* payload, std::size_t len) noexcept {
        using K = SequenceVerdict::Kind;
        Channel& c = channels_[h.channel];
        last_ = SequenceVerdict{};
        const std::uint32_t first = first_template(payload, len);
        if (h.seq_num == 0) {
            // Heartbeat: not sequenced; its nextSeqNo is checked when its
            // Sequence message is dispatched (check_heartbeat).
            ++counters_.heartbeats;
            if (first != layout::Sequence_2::template_id) ++counters_.heartbeat_without_sequence;
            last_.kind = K::heartbeat;
            return true;
        }
        ++counters_.sequenced;
        if (first == layout::SequenceReset_1::template_id) {
            // Accepted without checking its own sequenceNumber or version.
            ++counters_.resets;
            c.reset_pending = true;
            last_.kind = K::reset;
            return true;
        }
        if (c.reset_pending) {
            c.reset_pending = false;
            const std::uint16_t want = !c.seen ? h.seq_version
                                     : (c.version >= kMaxVersion ? 1 : static_cast<std::uint16_t>(c.version + 1));
            if (h.seq_version != want || h.seq_num != 1) ++counters_.reset_mismatches;
            c.seen = true;
            c.version = want;
            c.next = 1;   // then the ordinary rules: a later number is a gap
        }
        if (!c.seen) {
            // The first packet on a channel sets the expected number.
            c.seen = true;
            c.version = h.seq_version;
            c.next = h.seq_num + 1;
            last_.kind = K::first;
            return true;
        }
        if (h.seq_version != c.version) {
            ++counters_.version_changes;   // B3 changes version only through a SequenceReset
            c.version = h.seq_version;
            c.next = h.seq_num + 1;
            last_.kind = K::version_change;
            return true;
        }
        if (h.seq_num == c.next) {
            ++c.next;
            last_.kind = K::next;
            return true;
        }
        if (h.seq_num > c.next) {
            last_ = SequenceVerdict{K::gap, c.version, c.next, h.seq_num - c.next};
            counters_.gaps += last_.gap_count;
            ++counters_.gap_events;
            c.gaps[c.next_gap] = Gap{c.version, c.next, h.seq_num - 1};
            c.next_gap = (c.next_gap + 1) % kRecordedGaps;
            c.next = h.seq_num + 1;
            return true;
        }
        // At or below the last accepted: dropped, no dispatch, no event state.
        // Inside a gap this channel recorded, it is a late packet, not a duplicate.
        for (const Gap& g : c.gaps) {
            if (g.version == h.seq_version && g.first != 0 && g.first <= h.seq_num && h.seq_num <= g.last) {
                ++counters_.late_after_gap;
                last_.kind = K::late;
                return false;
            }
        }
        ++counters_.duplicates;
        last_.kind = K::duplicate;
        return false;
    }

    // A heartbeat's Sequence message: nextSeqNo must be the channel's next
    // expected number (1 after a reset packet). Nothing to check before the
    // channel's first sequenced packet.
    void check_heartbeat(std::uint8_t channel, std::uint32_t next_seq_no) noexcept {
        const Channel& c = channels_[channel];
        if (!c.seen && !c.reset_pending) return;
        const std::uint32_t expected = c.reset_pending ? 1 : c.next;
        if (next_seq_no != expected) ++counters_.heartbeat_mismatches;
    }

    // A channel's sequence version and next expected number (0, 0 before its
    // first sequenced packet): the silence rule's stale point.
    std::uint16_t expected_version(std::uint8_t channel) const noexcept { return channels_[channel].version; }
    std::uint32_t expected_next(std::uint8_t channel) const noexcept { return channels_[channel].next; }

    const SequenceVerdict& last() const noexcept { return last_; }
    const SequencerCounters& counters() const noexcept { return counters_; }

private:
    struct Gap {
        std::uint16_t version = 0;
        std::uint32_t first = 0;      // 0: an empty slot (sequenceNumber 0 is never sequenced)
        std::uint32_t last = 0;
    };
    struct Channel {
        bool seen = false;            // a sequenced packet was accepted
        bool reset_pending = false;   // a reset packet arrived; the next packet starts a version
        std::uint16_t version = 0;
        std::uint32_t next = 0;       // the next expected sequenceNumber
        std::array<Gap, kRecordedGaps> gaps{};   // the last gaps recorded, a ring
        std::size_t next_gap = 0;
    };
    std::array<Channel, 256> channels_{};
    SequencerCounters counters_;
    SequenceVerdict last_;
};

template <class Seq = Sequencer>
class PacketDecoder {
public:
    explicit PacketDecoder(Seq seq = Seq{}) : seq_(seq) {}

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
        if (!seq_.accept(out, payload, len)) {
            ++counters_.rejected_packets;
            return false;
        }
        return true;
    }

    const PacketCounters& counters() const noexcept { return counters_; }
    Seq& sequencer() noexcept { return seq_; }
    const Seq& sequencer() const noexcept { return seq_; }

private:
    Seq seq_;
    PacketCounters counters_;
};

}  // namespace b3::packet
