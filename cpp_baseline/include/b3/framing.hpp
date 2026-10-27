// framing.hpp: the "complete message" test, shared by the walker and the sequencer.
//
// What it does:
//   check() applies the tests of docs/DESIGN.md 3.6.1 to the message starting
//   at msg, with `remaining` bytes left in the datagram, in their fixed order,
//   and returns the first failure, or nothing for a complete message (its
//   header fields filled in). The walker uses it to step through a packet;
//   the sequencer uses it to look ahead for SequenceReset and a heartbeat's
//   nextSeqNo, so both agree on which messages exist.
//
// What it must not do:
//   Look at a message body, or assume a template's length.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string_view>

#include "b3/layouts.hpp"
#include "b3/wire.hpp"

namespace b3::framing {

namespace mh = layout::message_header;

// Why a packet's walk stops early (DESIGN.md 3.6.1), in test order.
enum class Malformed : std::uint8_t {
    header_truncated,
    length_zero,
    length_too_small,
    length_past_end,
    bad_encoding,
    bad_schema_id,
    block_length_past_end,
};
inline constexpr std::size_t kMalformedReasons = 7;
inline constexpr std::array<std::string_view, kMalformedReasons> kMalformedNames = {
    "header_truncated", "length_zero", "length_too_small", "length_past_end",
    "bad_encoding", "bad_schema_id", "block_length_past_end",
};

// msg_index values port 1 can hold (one byte, DESIGN.md 3.1): no packet has
// more messages than this passed on (DESIGN.md 3.6.1).
inline constexpr std::size_t kMaxMessagesPerPacket = 256;

struct MessageHeader {
    std::uint16_t length = 0;
    std::uint16_t encoding = 0;
    std::uint16_t block_length = 0;
    std::uint16_t template_id = 0;
    std::uint16_t schema_id = 0;
    std::uint16_t version = 0;
};

// The complete message test. On success fills h and returns nothing.
inline std::optional<Malformed> check(const std::uint8_t* msg, std::size_t remaining,
                                      MessageHeader& h) noexcept {
    if (remaining < mh::size) return Malformed::header_truncated;              // 1
    h.length = wire::load_le<mh::messageLength::type>(msg + mh::messageLength::offset);
    h.encoding = wire::load_le<mh::encodingType::type>(msg + mh::encodingType::offset);
    h.block_length = wire::load_le<mh::blockLength::type>(msg + mh::blockLength::offset);
    h.template_id = wire::load_le<mh::templateId::type>(msg + mh::templateId::offset);
    h.schema_id = wire::load_le<mh::schemaId::type>(msg + mh::schemaId::offset);
    h.version = wire::load_le<mh::version::type>(msg + mh::version::offset);
    if (h.length == 0) return Malformed::length_zero;                            // 2
    if (h.length < mh::size) return Malformed::length_too_small;                // 3
    if (h.length > remaining) return Malformed::length_past_end;                // 4
    if (h.encoding != layout::encoding_type) return Malformed::bad_encoding;    // 5
    if (h.schema_id != layout::schema_id) return Malformed::bad_schema_id;      // 6
    if (h.block_length > h.length - mh::size) return Malformed::block_length_past_end;  // 7
    return std::nullopt;
}

// Complete messages in one datagram, the way the walker counts them (up to
// kMaxMessagesPerPacket, stopping at the first failed test). Used at load
// time for an upper bound on output records.
inline std::size_t count_complete_messages(const std::uint8_t* payload, std::size_t len) noexcept {
    std::size_t pos = layout::packet_header::size;
    std::size_t n = 0;
    MessageHeader h;
    while (pos < len && n < kMaxMessagesPerPacket && !check(payload + pos, len - pos, h)) {
        pos += h.length;
        ++n;
    }
    return n;
}

}  // namespace b3::framing
