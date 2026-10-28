// event_sink.hpp: port 1. One 64-byte record per hot-path message.
//
// What it does:
//   A walker sink. For each dispatched message it fills the port 1 record of
//   docs/DESIGN.md 3.4 (kind, side, rptSeq, instrument, price, qty, order_id,
//   exch_time, aux per kind; absent fields as their null, section 1) and runs
//   the per-channel event-closing state machine of DESIGN.md 3.5:
//     * a message carrying EndOfEvent that is on port 1: its record has flag
//       bit 0 set
//     * a message carrying EndOfEvent that is not on port 1, with an event
//       open on its channel: an END_OF_EVENT marker in its place
//     * EndOfEvent with no event open and no port 1 record: nothing
//     * matchEventIndicator without EndOfEvent, or an ExecutionSummary: the
//       channel's event is open
//     * no matchEventIndicator (heartbeats, unknown templates): no state change
//     * SequenceReset or ChannelReset: its own record, then the channel's
//       event state is cleared (IDLE), with nothing else emitted
//   Duplicates and discarded tails never reach a sink, so they produce nothing.
//   With a tracker, a record whose instrument is a table instrument that is
//   not CLEAN (today: STALE), in the state the tracker left after this
//   message, carries flag bit 3 (DESIGN.md 3.3). Run the tracker first.
//   Records go into a buffer allocated by the caller before the loop, with a
//   capacity from a counting pass at load time; a record past it is counted
//   (overflow), never stored, and the buffer never grows.
//
// What it must not do:
//   Allocate, do I/O, make virtual calls, or read past a message's root
//   block. Track touched instruments (port 2, step 8).

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>

#include "b3/decode.hpp"
#include "b3/fields.hpp"
#include "b3/records.hpp"
#include "b3/tracker.hpp"
#include "b3/walker.hpp"

namespace b3::event_sink {

namespace F = fields;
namespace R = records;
namespace L = layout;

inline constexpr std::string_view kMatchEventIndicator = "matchEventIndicator";
inline constexpr std::uint8_t kMeiEndOfEvent = 0x80;   // matchEventIndicator bit 7
inline constexpr std::uint8_t kMeiRecovery = 0x20;     // bit 5
inline constexpr std::uint8_t kMeiImplied = 0x10;      // bit 4

using decode::TimeField;

// A root-block field's wire value; an absent field gives its null (0 for a
// required field), as DESIGN.md section 1 states.
template <class Fd>
inline typename Fd::type value_or_null(const walker::MessageView& m) noexcept {
    const auto v = decode::get<Fd>(m);
    if (v.absent()) {
        if constexpr (Fd::optional) return Fd::null_value;
        else return typename Fd::type{};
    }
    return v.value;
}

inline std::uint8_t side_of(char md_entry_type) noexcept {
    return md_entry_type == '0' ? R::side::BID : md_entry_type == '1' ? R::side::OFFER : R::side::NONE;
}

inline std::uint8_t flags_of(std::uint8_t mei) noexcept {
    return static_cast<std::uint8_t>(((mei & kMeiEndOfEvent) ? R::flag::END_OF_EVENT : 0) |
                                     ((mei & kMeiRecovery) ? R::flag::RECOVERY : 0) |
                                     ((mei & kMeiImplied) ? R::flag::IMPLIED : 0));
}

// The SESSION record's aux word (DESIGN.md 3.4): bytes 0..7 = session ID,
// sub ID or status, trading event, tradeDate (2 bytes LE), securityGroup.
inline std::uint64_t session_word(std::uint8_t session, std::uint8_t sub_or_status, std::uint8_t event,
                                  std::uint16_t trade_date, std::string_view group) noexcept {
    std::uint64_t w = session | (std::uint64_t{sub_or_status} << 8) | (std::uint64_t{event} << 16) |
                      (std::uint64_t{trade_date} << 24);
    for (std::size_t i = 0; i < 3 && i < group.size(); ++i) {
        w |= std::uint64_t{static_cast<unsigned char>(group[i])} << (40 + 8 * i);
    }
    return w;
}

// Fills r's kind-specific fields for template Id. Returns false when the
// message is not on port 1 (DESIGN.md 3.2).
template <std::uint16_t Id>
inline bool fill(const walker::MessageView& m, R::EventRecord& r) noexcept {
    if constexpr (Id == L::Order_MBO_50::template_id) {
        namespace T = F::Order_MBO_50;
        const auto action = value_or_null<T::mDUpdateAction>(m);
        if (action != L::enums::MDUpdateAction::NEW && action != L::enums::MDUpdateAction::CHANGE) return false;
        r.side = side_of(value_or_null<T::mDEntryType>(m));
        r.rpt_seq = value_or_null<T::rptSeq>(m);
        r.instrument_id = value_or_null<T::securityID>(m);
        r.price = value_or_null<T::mDEntryPx>(m);
        r.qty = value_or_null<T::mDEntrySize>(m);
        r.order_id = value_or_null<T::secondaryOrderID>(m);
        r.exch_time = value_or_null<T::transactTime>(m);
        if (action == L::enums::MDUpdateAction::NEW) {
            r.kind = R::kind::ADD;
            r.aux = value_or_null<T::enteringFirm>(m);
        } else {
            r.kind = R::kind::MODIFY;
            r.aux = static_cast<std::uint64_t>(value_or_null<T::mDEntryPrevSize>(m));   // two's complement
        }
        return true;
    } else if constexpr (Id == L::DeleteOrder_MBO_51::template_id) {
        namespace T = F::DeleteOrder_MBO_51;
        r.kind = R::kind::DELETE;
        r.side = side_of(value_or_null<T::mDEntryType>(m));
        r.rpt_seq = value_or_null<T::rptSeq>(m);
        r.instrument_id = value_or_null<T::securityID>(m);
        r.price = value_or_null<T::mDEntryPx>(m);
        r.qty = value_or_null<T::mDEntrySize>(m);
        r.order_id = value_or_null<T::secondaryOrderID>(m);
        r.exch_time = value_or_null<T::transactTime>(m);
        return true;
    } else if constexpr (Id == L::MassDeleteOrders_MBO_52::template_id) {
        namespace T = F::MassDeleteOrders_MBO_52;
        if (value_or_null<T::mDUpdateAction>(m) != L::enums::MDUpdateAction::DELETE_THRU) return false;
        r.kind = R::kind::CLEAR_SIDE;
        r.side = side_of(value_or_null<T::mDEntryType>(m));
        r.rpt_seq = value_or_null<T::rptSeq>(m);
        r.instrument_id = value_or_null<T::securityID>(m);
        r.exch_time = value_or_null<T::transactTime>(m);
        return true;
    } else if constexpr (Id == L::EmptyBook_9::template_id) {
        r.kind = R::kind::CLEAR_BOOK;
        r.instrument_id = value_or_null<F::EmptyBook_9::securityID>(m);
        r.exch_time = value_or_null<F::EmptyBook_9::mDEntryTimestamp>(m);
        return true;
    } else if constexpr (Id == L::ChannelReset_11::template_id) {
        r.kind = R::kind::CLEAR_CHANNEL;
        r.exch_time = value_or_null<F::ChannelReset_11::mDEntryTimestamp>(m);
        return true;
    } else if constexpr (Id == L::ExecutionSummary_55::template_id) {
        namespace T = F::ExecutionSummary_55;
        r.kind = R::kind::EXEC_SUMMARY;
        r.rpt_seq = value_or_null<T::rptSeq>(m);
        r.instrument_id = value_or_null<T::securityID>(m);
        r.price = value_or_null<T::lastPx>(m);
        r.qty = value_or_null<T::fillQty>(m);
        r.exch_time = value_or_null<T::transactTime>(m);
        r.aux = value_or_null<T::aggressorSide>(m);
        return true;
    } else if constexpr (Id == L::Trade_53::template_id) {
        namespace T = F::Trade_53;
        r.kind = R::kind::TRADE;
        r.rpt_seq = value_or_null<T::rptSeq>(m);
        r.instrument_id = value_or_null<T::securityID>(m);
        r.price = value_or_null<T::mDEntryPx>(m);
        r.qty = value_or_null<T::mDEntrySize>(m);
        r.order_id = value_or_null<T::tradeID>(m);
        r.exch_time = value_or_null<T::transactTime>(m);
        r.aux = (std::uint64_t{value_or_null<T::mDEntryBuyer>(m)} << 32) | value_or_null<T::mDEntrySeller>(m);
        return true;
    } else if constexpr (Id == L::TradeBust_57::template_id) {
        namespace T = F::TradeBust_57;
        r.kind = R::kind::TRADE_BUST;
        r.rpt_seq = value_or_null<T::rptSeq>(m);
        r.instrument_id = value_or_null<T::securityID>(m);
        r.price = value_or_null<T::mDEntryPx>(m);
        r.qty = value_or_null<T::mDEntrySize>(m);
        r.order_id = value_or_null<T::tradeID>(m);
        r.exch_time = value_or_null<T::transactTime>(m);
        return true;
    } else if constexpr (Id == L::SecurityGroupPhase_10::template_id) {
        namespace T = F::SecurityGroupPhase_10;
        r.kind = R::kind::SESSION;
        r.exch_time = value_or_null<T::transactTime>(m);
        const auto group = decode::get<T::securityGroup>(m);
        r.aux = session_word(value_or_null<T::tradingSessionID>(m), value_or_null<T::tradingSessionSubID>(m),
                             value_or_null<T::securityTradingEvent>(m), value_or_null<T::tradeDate>(m),
                             group.absent() ? std::string_view() : group.value);
        return true;
    } else if constexpr (Id == L::SecurityStatus_3::template_id) {
        namespace T = F::SecurityStatus_3;
        r.kind = R::kind::SESSION;
        r.rpt_seq = value_or_null<T::rptSeq>(m);
        r.instrument_id = value_or_null<T::securityID>(m);
        r.exch_time = value_or_null<T::transactTime>(m);
        r.aux = session_word(value_or_null<T::tradingSessionID>(m), value_or_null<T::securityTradingStatus>(m),
                             value_or_null<T::securityTradingEvent>(m), value_or_null<T::tradeDate>(m), {});
        return true;
    } else if constexpr (Id == L::SequenceReset_1::template_id) {
        r.kind = R::kind::SEQ_RESET;
        return true;
    } else {
        (void)m;
        (void)r;
        return false;   // not on port 1: statistics, News, heartbeats, ...
    }
}

struct EventCounters {
    std::uint64_t records = 0;            // stored
    std::uint64_t markers = 0;            // END_OF_EVENT markers among them
    std::uint64_t overflow = 0;           // records past the buffer's capacity: dropped, counted
    std::uint64_t resets_with_event_open = 0;   // SequenceReset or ChannelReset that cleared an open event
};

class EventSink {
public:
    EventSink(R::EventRecord* buffer, std::size_t capacity, const tracker::Tracker* tracker = nullptr) noexcept
        : buf_(buffer), cap_(capacity), tracker_(tracker) {}

    template <std::uint16_t Id>
    void on(const walker::MessageView& m) noexcept {
        using T = fields::Template<Id>;
        const std::uint8_t ch = m.packet.channel;
        R::EventRecord r;
        const bool on_port1 = fill<Id>(m, r);
        if constexpr (Id == L::SequenceReset_1::template_id || Id == L::ChannelReset_11::template_id) {
            // DESIGN.md 3.5: a reset clears the channel's event state and emits
            // nothing but its own record (flags from its matchEventIndicator).
            if (open_[ch]) ++counters_.resets_with_event_open;
            open_[ch] = false;
            identity(m, r);
            if constexpr (decode::has_named<kMatchEventIndicator>(typename T::fields{})) {
                const auto v = decode::get_named<kMatchEventIndicator>(typename T::fields{}, decode::root(m));
                r.flags = v.absent() ? 0 : flags_of(static_cast<std::uint8_t>(v.value));
            }
            emit(r);
            return;
        }
        bool has_mei = false;
        std::uint8_t mei = 0;
        if constexpr (decode::has_named<kMatchEventIndicator>(typename T::fields{})) {
            const auto v = decode::get_named<kMatchEventIndicator>(typename T::fields{}, decode::root(m));
            has_mei = !v.absent();
            mei = static_cast<std::uint8_t>(v.value);
        }
        if (on_port1) {
            identity(m, r);
            r.flags = static_cast<std::uint8_t>((has_mei ? flags_of(mei) : 0) | not_clean(r.instrument_id));
        }
        if (!has_mei) {
            if constexpr (Id == L::ExecutionSummary_55::template_id) open_[ch] = true;   // first of an event
            if (on_port1) emit(r);
            return;
        }
        if (mei & kMeiEndOfEvent) {
            const bool was_open = open_[ch];
            open_[ch] = false;
            if (on_port1) {
                emit(r);                                   // carries flag bit 0
            } else if (was_open) {
                R::EventRecord marker;
                identity(m, marker);
                marker.kind = R::kind::END_OF_EVENT;
                marker.flags = flags_of(mei);
                if constexpr (!TimeField<Id>::name.empty()) {
                    marker.exch_time = decode::get_named<TimeField<Id>::name>(typename T::fields{},
                                                                              decode::root(m)).value;
                }
                ++counters_.markers;
                emit(marker);
            }
            return;
        }
        open_[ch] = true;
        if (on_port1) emit(r);
    }

    // An unknown template: no matchEventIndicator, not on port 1, no state change.
    void on_unknown(const walker::MessageView&) noexcept {}

    std::size_t size() const noexcept { return count_; }
    const R::EventRecord* data() const noexcept { return buf_; }
    const EventCounters& counters() const noexcept { return counters_; }
    bool event_open(std::uint8_t channel) const noexcept { return open_[channel]; }

private:
    static void identity(const walker::MessageView& m, R::EventRecord& r) noexcept {
        r.seq_num = m.packet.seq_num;
        r.seq_version = m.packet.seq_version;
        r.channel = m.packet.channel;
        r.msg_index = static_cast<std::uint8_t>(m.msg_index);   // <= 255: walker.hpp guarantees it
        r.venue_msg_type = static_cast<std::uint8_t>(m.template_id);   // < 256: walker.hpp static_assert
    }

    // Bit 3 for a record of a table instrument that is not CLEAN. Records
    // with no instrument (channel-wide SESSION, CLEAR_CHANNEL, SEQ_RESET,
    // END_OF_EVENT) have instrument_id 0 and get 0.
    std::uint8_t not_clean(std::uint64_t instrument_id) const noexcept {
        if (tracker_ == nullptr || instrument_id == 0) return 0;
        const std::size_t slot = tracker_->table().find(instrument_id);
        return slot != instruments::Table::kNotFound && tracker_->state(slot).stale ? R::flag::BOOK_NOT_CLEAN : 0;
    }

    void emit(const R::EventRecord& r) noexcept {
        if (count_ < cap_) {
            buf_[count_++] = r;
            ++counters_.records;
        } else {
            ++counters_.overflow;
        }
    }

    R::EventRecord* buf_;
    std::size_t cap_;
    const tracker::Tracker* tracker_;
    std::size_t count_ = 0;
    std::array<bool, 256> open_{};
    EventCounters counters_;
};

}  // namespace b3::event_sink
