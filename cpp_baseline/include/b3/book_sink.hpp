// book_sink.hpp: port 2. Book records from port 1 records and state changes.
//
// What it does:
//   Implements docs/DESIGN.md 4.3. For each message, given its port 1
//   record (if any) and the tracker's state changes for it:
//     1. apply the record to the book (book_uncapped.hpp) and update the
//        channel's touched set (table instruments with a book-changing
//        record since the channel's last close, in order of first touch)
//     2. write a STATE_CHANGE per state change (levels if the new state is
//        CLEAN, zeros otherwise)
//     3. at a close (flag bit 0, not a reset), write a BOOK_UPDATE per
//        touched instrument that is CLEAN (or, with PublishStale, STALE too,
//        marked STALE), then empty the touched set.
//   A SequenceReset or ChannelReset closes nothing: it empties the touched
//   set; a ChannelReset then clears every table book of its channel and
//   touches each. A loss gives STATE_CHANGEs only. Book is UncappedBook or
//   CappedBook (the same interface); BOOK_UPDATE and STATE_CHANGE carry the
//   book's depth-incomplete flags (always 0 for the uncapped book). A STATUS is written when
//   capture time reaches a tick: one for the latest tick due, with the
//   number of earlier ticks it stands for (ticks_skipped), before the packet. Every record gets the next output sequence number.
//   Records go into a buffer allocated before the loop; past its capacity
//   they are counted, never stored.
//
// What it must not do:
//   Read the wire (port 2 is computed from port 1), allocate, do I/O.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <type_traits>

#include "b3/book_capped.hpp"
#include "b3/book_params.hpp"
#include "b3/book_uncapped.hpp"
#include "b3/instruments.hpp"
#include "b3/records.hpp"
#include "b3/tracker.hpp"

namespace b3::book {

namespace R = records;

struct BookSinkCounters {
    std::uint64_t book_updates = 0;
    std::uint64_t state_changes = 0;
    std::uint64_t statuses = 0;
    std::uint64_t overflow = 0;        // records past the buffer's capacity: dropped, counted
    std::uint64_t untracked_records = 0;   // book-changing records of instruments not in the table
};

// The book of this build (B3_BOOK).
using BuildBook = std::conditional_t<kBookMode == BookMode::Capped, CappedBook<>, UncappedBook>;

template <class Book = BuildBook, bool PublishStale = kPublishStale>
class BookSink {
public:
    BookSink(const instruments::Table& table, const tracker::Tracker& tracker, Book& book,
             R::BookRecord* buffer, std::size_t capacity, std::uint64_t status_interval_ns) noexcept
        : table_(table), tracker_(tracker), book_(book), buf_(buffer), cap_(capacity),
          interval_(status_interval_ns) {}

    // Before each datagram: when its capture time reaches one or more ticks,
    // one STATUS for the latest, ticks_skipped (offset 24) the earlier ones.
    void begin_datagram(std::uint64_t capture_time) noexcept {
        if (!started_) {
            started_ = true;
            next_tick_ = interval_ ? capture_time + interval_ : 0;
        }
        if (interval_ && capture_time >= next_tick_) {
            const std::uint64_t due = (capture_time - next_tick_) / interval_ + 1;
            const std::uint64_t latest = next_tick_ + (due - 1) * interval_;
            R::BookRecord r{};
            r.header.record_type = R::record_type::STATUS;
            r.header.capture_time = latest;
            r.header.exch_time = due - 1;   // ticks_skipped
            ++counters_.statuses;
            emit(r);
            next_tick_ = latest + interval_;
        }
        capture_time_ = capture_time;
        book_.set_time(capture_time);
    }

    // One message: its port 1 records (0 or 1, from the event sink) and the
    // tracker's state changes for it.
    void on_message(const R::EventRecord* records, std::size_t n_records, const tracker::Transition* transitions,
                    std::size_t n_transitions) noexcept {
        const R::EventRecord* closer = nullptr;
        for (std::size_t i = 0; i < n_records; ++i) {
            if (apply(records[i])) closer = &records[i];
        }
        on_transitions(transitions, n_transitions);
        if (closer != nullptr) close(*closer);
    }

    // State changes with no message of their own (a loss).
    void on_transitions(const tracker::Transition* t, std::size_t n) noexcept {
        for (std::size_t i = 0; i < n; ++i) state_change(t[i]);
    }

    const BookSinkCounters& counters() const noexcept { return counters_; }
    std::size_t size() const noexcept { return count_; }
    const R::BookRecord* data() const noexcept { return buf_; }

private:
    struct Touched {
        std::array<std::uint8_t, kMaxInstruments> slots{};   // in order of first touch
        std::array<bool, kMaxInstruments> in{};
        std::array<bool, kMaxInstruments> recovery{};       // a change in the event had RecoveryMsg
        std::size_t n = 0;
        void clear() noexcept {
            for (std::size_t i = 0; i < n; ++i) {
                in[slots[i]] = false;
                recovery[slots[i]] = false;
            }
            n = 0;
        }
        void add(std::size_t slot, bool rec) noexcept {
            if (!in[slot]) {
                in[slot] = true;
                slots[n++] = static_cast<std::uint8_t>(slot);
            }
            recovery[slot] = recovery[slot] || rec;
        }
    };
    static_assert(kMaxInstruments <= 256, "touched slots are stored in one byte");

    // Applies one port 1 record. Returns true when it closes the event.
    bool apply(const R::EventRecord& r) noexcept {
        Touched& touched = touched_[r.channel];
        if (r.kind == R::kind::SEQ_RESET || r.kind == R::kind::CLEAR_CHANNEL) {
            touched.clear();   // the open event is discarded: no BOOK_UPDATE
            if (r.kind == R::kind::CLEAR_CHANNEL) {
                for (std::size_t slot = 0; slot < table_.size(); ++slot) {
                    if (table_[slot].channel != r.channel) continue;
                    book_.clear(slot);
                    touched.add(slot, (r.flags & R::flag::RECOVERY) != 0);
                }
            }
            return false;      // a reset closes nothing
        }
        const bool changes_book = r.kind == R::kind::ADD || r.kind == R::kind::MODIFY ||
                                  r.kind == R::kind::DELETE || r.kind == R::kind::CLEAR_SIDE ||
                                  r.kind == R::kind::CLEAR_BOOK;
        if (changes_book) {
            const std::size_t slot = table_.find(r.instrument_id);
            if (slot == instruments::Table::kNotFound) {
                ++counters_.untracked_records;
            } else {
                const bool stale = tracker_.state(slot).stale;
                const Side side = r.side == R::side::OFFER ? Side::offer : Side::bid;
                switch (r.kind) {
                    case R::kind::ADD: book_.add(slot, side, r.price, r.qty, stale); break;
                    case R::kind::MODIFY:
                        book_.change(slot, side, r.price, r.qty, static_cast<std::int64_t>(r.aux), stale);
                        break;
                    case R::kind::DELETE: book_.remove(slot, side, r.price, r.qty, stale); break;
                    case R::kind::CLEAR_SIDE: book_.clear_side(slot, side); break;
                    default: book_.clear(slot); break;   // CLEAR_BOOK
                }
                touched.add(slot, (r.flags & R::flag::RECOVERY) != 0);
            }
        }
        return (r.flags & R::flag::END_OF_EVENT) != 0;
    }

    void close(const R::EventRecord& closer) noexcept {
        Touched& touched = touched_[closer.channel];
        for (std::size_t i = 0; i < touched.n; ++i) {
            const std::size_t slot = touched.slots[i];
            const bool stale = tracker_.state(slot).stale;
            if (stale && !PublishStale) continue;
            R::BookRecord r{};
            identity(r, closer.seq_num, closer.seq_version, closer.channel, closer.msg_index, closer.exch_time);
            r.header.instrument_id = table_[slot].security_id;
            r.header.record_type = R::record_type::BOOK_UPDATE;
            r.header.state = stale ? R::book_state::STALE : R::book_state::CLEAN;
            r.header.flags = touched.recovery[slot] ? R::book_flag::RECOVERY : 0;
            levels(r, slot);
            depth_flags(r, slot);
            ++counters_.book_updates;
            emit(r);
        }
        touched.clear();
    }

    void state_change(const tracker::Transition& t) noexcept {
        R::BookRecord r{};
        identity(r, t.at.seq_num, t.at.version, t.at.channel, t.msg_index, t.exch_time);
        r.header.instrument_id = table_[t.slot].security_id;
        r.header.record_type = R::record_type::STATE_CHANGE;
        r.header.state = t.to_stale ? R::book_state::STALE : R::book_state::CLEAN;
        r.header.cause = t.cause;
        if (!t.to_stale) levels(r, t.slot);
        depth_flags(r, t.slot);
        ++counters_.state_changes;
        emit(r);
    }

    void identity(R::BookRecord& r, std::uint32_t seq, std::uint16_t version, std::uint8_t channel,
                  std::uint8_t index, std::uint64_t exch_time) const noexcept {
        r.header.seq_num = seq;
        r.header.seq_version = version;
        r.header.channel = channel;
        r.header.msg_index = index;
        r.header.exch_time = exch_time;
        r.header.capture_time = capture_time_;
    }

    void levels(R::BookRecord& r, std::size_t slot) const noexcept {
        r.header.bid_count = static_cast<std::uint8_t>(book_.top(slot, Side::bid, r.bids, kPublishedDepth));
        r.header.offer_count = static_cast<std::uint8_t>(book_.top(slot, Side::offer, r.offers, kPublishedDepth));
    }

    void depth_flags(R::BookRecord& r, std::size_t slot) const noexcept {
        if (book_.depth_incomplete(slot, Side::bid)) r.header.flags |= R::book_flag::DEPTH_INCOMPLETE_BID;
        if (book_.depth_incomplete(slot, Side::offer)) r.header.flags |= R::book_flag::DEPTH_INCOMPLETE_OFFER;
    }

    void emit(R::BookRecord& r) noexcept {
        r.header.out_seq = ++out_seq_;
        if (count_ < cap_) {
            buf_[count_++] = r;
        } else {
            ++counters_.overflow;
        }
    }

    const instruments::Table& table_;
    const tracker::Tracker& tracker_;
    Book& book_;
    R::BookRecord* buf_;
    std::size_t cap_;
    std::size_t count_ = 0;
    std::uint64_t out_seq_ = 0;
    std::uint64_t interval_;
    std::uint64_t next_tick_ = 0;
    std::uint64_t capture_time_ = 0;
    bool started_ = false;
    std::array<Touched, 256> touched_{};
    BookSinkCounters counters_;
};

}  // namespace b3::book
