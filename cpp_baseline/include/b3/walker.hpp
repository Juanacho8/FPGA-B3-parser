// walker.hpp: the message walker. One packet in, one MessageView per message out.
//
// What it does:
//   Steps through the messages after the packet header using only each
//   message's messageLength. Before dispatching a message it applies the
//   "complete message" test of docs/DESIGN.md 3.6.1, in that order:
//     1. at least 12 bytes remain             else header_truncated
//     2. messageLength != 0                    else length_zero
//     3. messageLength >= 12                   else length_too_small
//     4. messageLength <= bytes remaining      else length_past_end
//     5. encodingType == 0xEB50                else bad_encoding
//     6. schemaId == 2                         else bad_schema_id
//     7. blockLength <= messageLength - 12     else block_length_past_end
//   On the first failure it counts the reason, stops and discards the rest
//   of the packet. Messages already dispatched from that packet stand.
//   A complete message goes to the sink by a switch on templateId: one case
//   per template in the schema, sink.on<templateId>(view). Any other
//   templateId goes to sink.on_unknown(view) and is counted; it is skipped by
//   messageLength like any message and is not malformed.
//
// What it must not do:
//   Assume a template's length, walk groups or var data (messageLength
//   already covers them; their bounds are step 5's), allocate, or make
//   virtual calls. The sink is a template parameter.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>

#include "b3/layouts.hpp"
#include "b3/packet.hpp"
#include "b3/wire.hpp"

namespace b3::walker {

namespace mh = layout::message_header;
namespace L = layout;

// One complete message, pointing into the packet buffer.
struct MessageView {
    packet::PacketHeader packet;   // header of the packet holding it
    std::uint16_t msg_index;       // 0-based position in the packet, as intent.jsonl's "index"
    std::uint16_t template_id;
    std::uint16_t version;
    std::uint16_t block_length;    // from the message header (trust it, not the schema)
    std::uint16_t message_length;  // whole message, 12-byte header included
    const std::uint8_t* message;   // the message header
    const std::uint8_t* body;      // message + 12: the root block, then groups and var data
};

// Why a packet's walk stopped early (DESIGN.md 3.6.1), in test order.
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

struct WalkCounters {
    std::uint64_t packets = 0;            // packets walked
    std::uint64_t empty_packets = 0;      // a header and no messages (not malformed)
    std::uint64_t messages = 0;           // complete messages dispatched, unknown included
    std::uint64_t unknown_templates = 0;  // of those, templateId not in the schema
    std::uint64_t malformed_packets = 0;  // walks stopped by a failed test
    std::array<std::uint64_t, kMalformedReasons> malformed{};  // by reason
};

// ---- Dispatch: one case per schema template ----
// Returns false for a templateId the schema does not have (sent to on_unknown).
template <class Sink>
inline bool dispatch(const MessageView& m, Sink& sink) {
    switch (m.template_id) {
        case L::SequenceReset_1::template_id: sink.template on<L::SequenceReset_1::template_id>(m); return true;
        case L::Sequence_2::template_id: sink.template on<L::Sequence_2::template_id>(m); return true;
        case L::SecurityStatus_3::template_id: sink.template on<L::SecurityStatus_3::template_id>(m); return true;
        case L::News_5::template_id: sink.template on<L::News_5::template_id>(m); return true;
        case L::EmptyBook_9::template_id: sink.template on<L::EmptyBook_9::template_id>(m); return true;
        case L::SecurityGroupPhase_10::template_id: sink.template on<L::SecurityGroupPhase_10::template_id>(m); return true;
        case L::ChannelReset_11::template_id: sink.template on<L::ChannelReset_11::template_id>(m); return true;
        case L::SecurityDefinition_12::template_id: sink.template on<L::SecurityDefinition_12::template_id>(m); return true;
        case L::OpeningPrice_15::template_id: sink.template on<L::OpeningPrice_15::template_id>(m); return true;
        case L::TheoreticalOpeningPrice_16::template_id: sink.template on<L::TheoreticalOpeningPrice_16::template_id>(m); return true;
        case L::ClosingPrice_17::template_id: sink.template on<L::ClosingPrice_17::template_id>(m); return true;
        case L::AuctionImbalance_19::template_id: sink.template on<L::AuctionImbalance_19::template_id>(m); return true;
        case L::QuantityBand_21::template_id: sink.template on<L::QuantityBand_21::template_id>(m); return true;
        case L::PriceBand_22::template_id: sink.template on<L::PriceBand_22::template_id>(m); return true;
        case L::HighPrice_24::template_id: sink.template on<L::HighPrice_24::template_id>(m); return true;
        case L::LowPrice_25::template_id: sink.template on<L::LowPrice_25::template_id>(m); return true;
        case L::LastTradePrice_27::template_id: sink.template on<L::LastTradePrice_27::template_id>(m); return true;
        case L::SettlementPrice_28::template_id: sink.template on<L::SettlementPrice_28::template_id>(m); return true;
        case L::OpenInterest_29::template_id: sink.template on<L::OpenInterest_29::template_id>(m); return true;
        case L::SnapshotFullRefresh_Header_30::template_id: sink.template on<L::SnapshotFullRefresh_Header_30::template_id>(m); return true;
        case L::Order_MBO_50::template_id: sink.template on<L::Order_MBO_50::template_id>(m); return true;
        case L::DeleteOrder_MBO_51::template_id: sink.template on<L::DeleteOrder_MBO_51::template_id>(m); return true;
        case L::MassDeleteOrders_MBO_52::template_id: sink.template on<L::MassDeleteOrders_MBO_52::template_id>(m); return true;
        case L::Trade_53::template_id: sink.template on<L::Trade_53::template_id>(m); return true;
        case L::ForwardTrade_54::template_id: sink.template on<L::ForwardTrade_54::template_id>(m); return true;
        case L::ExecutionSummary_55::template_id: sink.template on<L::ExecutionSummary_55::template_id>(m); return true;
        case L::ExecutionStatistics_56::template_id: sink.template on<L::ExecutionStatistics_56::template_id>(m); return true;
        case L::TradeBust_57::template_id: sink.template on<L::TradeBust_57::template_id>(m); return true;
        case L::SnapshotFullRefresh_Orders_MBO_71::template_id: sink.template on<L::SnapshotFullRefresh_Orders_MBO_71::template_id>(m); return true;
        default: sink.on_unknown(m); return false;
    }
}

// True when templateId is in the schema. tests/test_walker.cpp checks that
// dispatch() has a case for exactly these.
constexpr bool is_known_template(std::uint16_t id) noexcept {
    for (const auto t : L::template_ids) {
        if (t == id) return true;
    }
    return false;
}

// ---- The walk ----
class Walker {
public:
    // Walks the messages of one accepted packet (payload holds the whole
    // datagram, packet header included) and dispatches each complete one.
    template <class Sink>
    void walk(const packet::PacketHeader& header, const std::uint8_t* payload, std::size_t len,
              Sink& sink) noexcept {
        ++counters_.packets;
        std::size_t pos = layout::packet_header::size;
        if (pos >= len) {
            ++counters_.empty_packets;
            return;
        }
        std::uint16_t index = 0;
        while (pos < len) {
            const std::size_t remaining = len - pos;
            const std::uint8_t* msg = payload + pos;
            // 1. The 12-byte message header fits.
            if (remaining < mh::size) {
                fail(Malformed::header_truncated);
                return;
            }
            const auto length = wire::load_le<mh::messageLength::type>(msg + mh::messageLength::offset);
            const auto encoding = wire::load_le<mh::encodingType::type>(msg + mh::encodingType::offset);
            const auto block = wire::load_le<mh::blockLength::type>(msg + mh::blockLength::offset);
            const auto tid = wire::load_le<mh::templateId::type>(msg + mh::templateId::offset);
            const auto schema = wire::load_le<mh::schemaId::type>(msg + mh::schemaId::offset);
            const auto version = wire::load_le<mh::version::type>(msg + mh::version::offset);
            // 2-4. messageLength frames a whole message inside the datagram.
            if (length == 0) {
                fail(Malformed::length_zero);
                return;
            }
            if (length < mh::size) {
                fail(Malformed::length_too_small);
                return;
            }
            if (length > remaining) {
                fail(Malformed::length_past_end);
                return;
            }
            // 5-7. It is an SBE message of this schema, and its root block fits.
            if (encoding != layout::encoding_type) {
                fail(Malformed::bad_encoding);
                return;
            }
            if (schema != layout::schema_id) {
                fail(Malformed::bad_schema_id);
                return;
            }
            if (block > length - mh::size) {
                fail(Malformed::block_length_past_end);
                return;
            }

            const MessageView view{header, index, tid, version, block, length, msg, msg + mh::size};
            ++counters_.messages;
            if (!dispatch(view, sink)) {
                ++counters_.unknown_templates;
            }
            pos += length;
            ++index;
        }
    }

    const WalkCounters& counters() const noexcept { return counters_; }

private:
    void fail(Malformed reason) noexcept {
        ++counters_.malformed_packets;
        ++counters_.malformed[static_cast<std::size_t>(reason)];
    }

    WalkCounters counters_;
};

// ---- Step 4 sink: counts messages per templateId ----
struct CountingSink {
    std::array<std::uint64_t, 65536> per_template{};   // by templateId, unknown included

    template <std::uint16_t TemplateId>
    void on(const MessageView&) noexcept {
        ++per_template[TemplateId];
    }
    void on_unknown(const MessageView& m) noexcept { ++per_template[m.template_id]; }
};

}  // namespace b3::walker
