// tracker.hpp: per-instrument rptSeq and stale flags, and per-message checks.
//
// What it does:
//   A walker sink, run on every dispatched message (docs/DESIGN.md 3.6.4):
//   * a heartbeat's Sequence message: its nextSeqNo goes to the sequencer
//   * a SequenceReset that is not its packet's first message: counted, and
//     otherwise an ordinary message (only a first one makes a reset packet)
//   * a message with a securityID: looked up in the instrument table. Not in
//     the table: counted, no tracking. In the table: rptSeq must be the
//     previous + 1, or 1 on the message after its EmptyBook. A jump is
//     counted and marks that instrument stale.
//   * EmptyBook clears its instrument's stale flag; ChannelReset clears it
//     for every table instrument of its channel. rptSeq is not touched by a
//     ChannelReset: each instrument's EmptyBook restarts it.
//   * with walk_bodies on (--counters and --decode runs only), groups and
//     var data are bounds-checked (decode::walk_body) and body faults
//     counted. Off, no message body past its root block is read: the path
//     step 10 times, like the FPGA, steps over every message by messageLength.
//     Body faults are a software-only counter (DESIGN.md 3.6.2).
//   * a message for a table instrument after a ChannelReset on its channel
//     and before that instrument's EmptyBook is counted (msg_before_rebuild,
//     generator open item J).
//   on_loss() marks every table instrument of a channel stale: the caller
//   reports a sequence gap (at its first missing number) or a discarded
//   malformed tail (at the faulted packet). Flags only; there is no book yet.
//   No snapshot recovery: a loss with no later EmptyBook or ChannelReset
//   leaves an instrument stale to the end.
//
// What it must not do:
//   Allocate, make virtual calls, or keep per-order state.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>

#include "b3/book_params.hpp"
#include "b3/decode.hpp"
#include "b3/fields.hpp"
#include "b3/instruments.hpp"
#include "b3/packet.hpp"
#include "b3/walker.hpp"

namespace b3::tracker {

inline constexpr std::string_view kSecurityID = "securityID";
inline constexpr std::string_view kRptSeq = "rptSeq";

// Field Name of the descriptor list, read as an unsigned integer (absent if
// the template has no such field). Resolved at compile time.
template <const std::string_view& Name, class... Fs>
inline decode::Value<std::uint64_t> get_named(decode::List<Fs...>, const decode::Block& b) noexcept {
    decode::Value<std::uint64_t> out;
    (
        [&] {
            if constexpr (Fs::name == Name) {
                const auto v = decode::get<Fs>(b);
                out = {v.presence, static_cast<std::uint64_t>(v.value)};
            }
        }(),
        ...);
    return out;
}

template <const std::string_view& Name, class... Fs>
constexpr bool has_named(decode::List<Fs...>) noexcept {
    return ((Fs::name == Name) || ...);
}

struct StalePoint {
    std::uint8_t channel = 0;
    std::uint16_t version = 0;
    std::uint32_t seq_num = 0;
};

struct InstrumentState {
    bool have_rpt = false;        // a previous rptSeq (or an EmptyBook) is known
    std::uint32_t last_rpt = 0;   // 0 after EmptyBook: the next must be 1
    bool stale = false;
    bool ever_stale = false;
    bool awaiting_rebuild = false; // ChannelReset seen, its EmptyBook not yet
    StalePoint first_stale{};     // where it first went stale
    std::uint64_t rpt_seq_gaps = 0;
    std::uint64_t stale_events = 0;    // times it went from fresh to stale
};

struct TrackerCounters {
    std::uint64_t unknown_instrument_messages = 0;   // securityID present, not in the table
    std::uint64_t seq_reset_not_first = 0;
    std::uint64_t msg_before_rebuild = 0;             // after a ChannelReset, before its EmptyBook
    std::uint64_t losses = 0;                         // on_loss calls (gaps and discarded tails)
    std::array<std::uint64_t, decode::kBodyFaults> body_faults{};
};

class Tracker {
public:
    Tracker(const instruments::Table& table, packet::Sequencer& sequencer,
            bool walk_bodies = false) noexcept
        : table_(table), sequencer_(sequencer), walk_bodies_(walk_bodies) {}

    // A loss on a channel: every table instrument on it goes stale at point.
    void on_loss(const StalePoint& point) noexcept {
        ++counters_.losses;
        for (std::size_t slot = 0; slot < table_.size(); ++slot) {   // RTL: all slots at once
            if (table_[slot].channel == point.channel) mark_stale(slot, point);
        }
    }

    template <std::uint16_t TemplateId>
    void on(const walker::MessageView& m) noexcept {
        using T = fields::Template<TemplateId>;
        namespace L = layout;
        if constexpr (TemplateId == L::Sequence_2::template_id) {
            if (m.packet.seq_num == 0) {
                const auto next = decode::get<fields::Sequence_2::nextSeqNo>(m);
                if (next.present()) sequencer_.check_heartbeat(m.packet.channel, next.value);
            }
        } else if constexpr (TemplateId == L::SequenceReset_1::template_id) {
            if (m.msg_index > 0) ++counters_.seq_reset_not_first;
        } else if constexpr (TemplateId == L::ChannelReset_11::template_id) {
            for (std::size_t slot = 0; slot < table_.size(); ++slot) {
                if (table_[slot].channel == m.packet.channel) {
                    state_[slot].stale = false;
                    state_[slot].awaiting_rebuild = true;
                }
            }
        }
        if constexpr (has_named<kSecurityID>(typename T::fields{})) {
            const auto root = decode::root(m);
            const auto sid = get_named<kSecurityID>(typename T::fields{}, root);
            if (sid.present()) {
                const std::size_t slot = table_.find(sid.value);
                if (slot == instruments::Table::kNotFound) {
                    ++counters_.unknown_instrument_messages;
                } else if constexpr (TemplateId == L::EmptyBook_9::template_id) {
                    InstrumentState& st = state_[slot];
                    st.awaiting_rebuild = false;
                    st.stale = false;      // rebuilt from here
                    st.have_rpt = true;
                    st.last_rpt = 0;       // rptSeq restarts at 1 with the next message
                } else {
                    if (state_[slot].awaiting_rebuild) ++counters_.msg_before_rebuild;
                    if constexpr (has_named<kRptSeq>(typename T::fields{})) {
                        const auto rpt = get_named<kRptSeq>(typename T::fields{}, root);
                        if (rpt.present()) track_rpt(slot, static_cast<std::uint32_t>(rpt.value), m);
                    }
                }
            }
        }
        if constexpr (!std::is_same_v<typename T::groups, decode::List<>> ||
                      !std::is_same_v<typename T::var_data, decode::List<>>) {
            if (walk_bodies_) {
                BodyCheck body{counters_};
                decode::walk_body<T>(m, body);
            }
        }
    }

    void on_unknown(const walker::MessageView&) noexcept {}

    const TrackerCounters& counters() const noexcept { return counters_; }
    const InstrumentState& state(std::size_t slot) const noexcept { return state_[slot]; }
    const instruments::Table& table() const noexcept { return table_; }

private:
    struct BodyCheck {
        TrackerCounters& c;
        template <class G> void on_group(std::size_t, std::size_t) noexcept {}
        template <class G> void on_entry(std::size_t, const decode::Block&) noexcept {}
        template <class D> void on_var_data(std::string_view) noexcept {}
        template <class W> void on_body_fault(decode::BodyFault r) noexcept {
            ++c.body_faults[static_cast<std::size_t>(r)];
        }
    };

    void track_rpt(std::size_t slot, std::uint32_t rpt, const walker::MessageView& m) noexcept {
        InstrumentState& st = state_[slot];
        if (st.have_rpt && rpt != st.last_rpt + 1) {
            ++st.rpt_seq_gaps;   // a message for it was lost: just this instrument
            mark_stale(slot, StalePoint{m.packet.channel, m.packet.seq_version, m.packet.seq_num});
        }
        st.have_rpt = true;
        st.last_rpt = rpt;
    }

    void mark_stale(std::size_t slot, const StalePoint& point) noexcept {
        InstrumentState& st = state_[slot];
        if (!st.stale) ++st.stale_events;
        st.stale = true;
        if (!st.ever_stale) {
            st.ever_stale = true;
            st.first_stale = point;
        }
    }

    const instruments::Table& table_;
    packet::Sequencer& sequencer_;
    bool walk_bodies_;
    std::array<InstrumentState, kMaxInstruments> state_{};
    TrackerCounters counters_;
};

}  // namespace b3::tracker
