// mbo_check.hpp: the MBO book driven from port 1, checked against the uncapped book.
//
// What it does:
//   For each message, after the price-level book has taken it (book_sink.hpp),
//   applies its port 1 record to the MBO book (book_mbo.hpp), with the
//   instrument's state from the tracker, and keeps the same touched set as
//   port 2 (DESIGN.md 4.3): table instruments with a book-changing record
//   since the channel's last close; a SequenceReset or ChannelReset empties
//   it, a ChannelReset then touches every table instrument of its channel.
//   At every close (flag bit 0, not a reset), for every touched instrument
//   that is CLEAN, the MBO book's per-price totals (qty and order count,
//   every level, both sides) must equal the uncapped book's. Mismatches are
//   counted; the first few are kept with their identity.
//
// What it must not do:
//   Run in a timed build, read the wire, allocate after construction, or
//   change the price-level book.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

#include "b3/book_mbo.hpp"
#include "b3/book_uncapped.hpp"
#include "b3/instruments.hpp"
#include "b3/records.hpp"
#include "b3/tracker.hpp"

namespace b3::book {

struct MboMismatch {
    std::uint32_t seq_num = 0;        // the closing record's identity
    std::uint16_t seq_version = 0;
    std::uint8_t channel = 0;
    std::uint8_t msg_index = 0;
    std::uint64_t instrument_id = 0;
    Side side = Side::bid;
    std::size_t level = 0;            // first differing level, best first
    records::BookLevel mbo{};         // price 0, qty 0 when the side has no such level
    records::BookLevel levels{};      // the uncapped book's
};

struct MboCheckCounters {
    std::uint64_t closes = 0;            // event closes seen
    std::uint64_t instrument_checks = 0; // touched CLEAN instruments compared, both sides each
    std::uint64_t level_mismatches = 0;  // sides whose levels differ
};

class MboCheck {
public:
    static constexpr std::size_t kKeptMismatches = 8;

    MboCheck(const instruments::Table& table, const tracker::Tracker& tracker, MboBook& mbo,
             const UncappedBook& levels) noexcept
        : table_(table), tracker_(tracker), mbo_(mbo), levels_(levels),
          a_(mbo.capacity() + 1), b_(levels.levels_per_side() + 1) {}

    void on_message(const records::EventRecord* records, std::size_t n) noexcept {
        for (std::size_t i = 0; i < n; ++i) apply(records[i]);
    }

    const MboCheckCounters& counters() const noexcept { return counters_; }
    const MboMismatch* mismatches() const noexcept { return kept_.data(); }
    std::size_t kept() const noexcept { return n_kept_; }

private:
    struct Touched {
        std::array<std::uint8_t, kMaxInstruments> slots{};   // in order of first touch
        std::array<bool, kMaxInstruments> in{};
        std::size_t n = 0;
        void clear() noexcept {
            for (std::size_t i = 0; i < n; ++i) in[slots[i]] = false;
            n = 0;
        }
        void add(std::size_t slot) noexcept {
            if (!in[slot]) {
                in[slot] = true;
                slots[n++] = static_cast<std::uint8_t>(slot);
            }
        }
    };
    static_assert(kMaxInstruments <= 256, "touched slots are stored in one byte");

    void apply(const records::EventRecord& r) noexcept {
        namespace K = records::kind;
        Touched& touched = touched_[r.channel];
        if (r.kind == K::SEQ_RESET || r.kind == K::CLEAR_CHANNEL) {
            touched.clear();   // the open event is discarded
            if (r.kind == K::CLEAR_CHANNEL) {
                for (std::size_t slot = 0; slot < table_.size(); ++slot) {
                    if (table_[slot].channel != r.channel) continue;
                    mbo_.clear(slot);
                    touched.add(slot);
                }
            }
            return;            // a reset closes nothing
        }
        if (r.kind == K::ADD || r.kind == K::MODIFY || r.kind == K::DELETE || r.kind == K::CLEAR_SIDE ||
            r.kind == K::CLEAR_BOOK) {
            const std::size_t slot = table_.find(r.instrument_id);
            if (slot != instruments::Table::kNotFound) {
                const bool stale = tracker_.state(slot).stale;
                const Side side = r.side == records::side::OFFER ? Side::offer : Side::bid;
                switch (r.kind) {
                    case K::ADD: mbo_.add(slot, side, r.price, r.qty, r.order_id, stale); break;
                    case K::MODIFY:
                        mbo_.modify(slot, side, r.price, r.qty, static_cast<std::int64_t>(r.aux), r.order_id, stale);
                        break;
                    case K::DELETE: mbo_.remove(slot, side, r.price, r.qty, r.order_id, stale); break;
                    case K::CLEAR_SIDE: mbo_.clear_side(slot, side); break;
                    default: mbo_.clear(slot); break;   // CLEAR_BOOK
                }
                touched.add(slot);
            }
        }
        if ((r.flags & records::flag::END_OF_EVENT) != 0) close(r, touched);
    }

    void close(const records::EventRecord& closer, Touched& touched) noexcept {
        ++counters_.closes;
        for (std::size_t i = 0; i < touched.n; ++i) {
            const std::size_t slot = touched.slots[i];
            if (tracker_.state(slot).stale) continue;
            ++counters_.instrument_checks;
            for (const Side side : {Side::bid, Side::offer}) compare(closer, slot, side);
        }
        touched.clear();
    }

    void compare(const records::EventRecord& closer, std::size_t slot, Side side) noexcept {
        const std::size_t na = mbo_.levels(slot, side, a_.data(), a_.size());
        const std::size_t nb = levels_.top(slot, side, b_.data(), b_.size());
        const std::size_t n = na > nb ? na : nb;
        for (std::size_t i = 0; i < n; ++i) {
            const records::BookLevel x = i < na ? a_[i] : records::BookLevel{};
            const records::BookLevel y = i < nb ? b_[i] : records::BookLevel{};
            if (x.price == y.price && x.qty == y.qty && x.order_count == y.order_count) continue;
            ++counters_.level_mismatches;
            if (n_kept_ < kKeptMismatches) {
                kept_[n_kept_++] = MboMismatch{closer.seq_num, closer.seq_version, closer.channel, closer.msg_index,
                                               table_[slot].security_id, side, i, x, y};
            }
            return;   // one count per side
        }
    }

    const instruments::Table& table_;
    const tracker::Tracker& tracker_;
    MboBook& mbo_;
    const UncappedBook& levels_;
    std::vector<records::BookLevel> a_;   // scratch: the MBO book's levels of one side
    std::vector<records::BookLevel> b_;   // scratch: the uncapped book's
    std::array<Touched, 256> touched_{};
    std::array<MboMismatch, kKeptMismatches> kept_{};
    std::size_t n_kept_ = 0;
    MboCheckCounters counters_;
};

}  // namespace b3::book
