// book_sink.hpp: port 2. Book records from port 1 records and state changes.
//
// What it does:
//   Implements docs/DESIGN.md 4.3. Keeps the book state per instrument
//   (DESIGN.md 3.6.4): STALE when the tracker's feed state is not clean or a
//   consistency fault happened (cause 8), cleared by the instrument's
//   EmptyBook or a ChannelReset. For each message, given its port 1 record
//   (if any) and the tracker's feed-state changes for it:
//     1. apply the record to the book (book_uncapped.hpp) and update the
//        channel's touched set (table instruments with a book-changing
//        record since the channel's last close, in order of first touch); a
//        consistency error while the book is CLEAN is a fault
//     2. write a STATE_CHANGE per book-state change (levels if the new state
//        is CLEAN, zeros otherwise): a feed change takes its cause, a fault
//        cause 8, a clear of a faulted-only book its EmptyBook or ChannelReset
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
#include <utility>

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
    std::uint64_t consistency_faults = 0;  // books gone STALE on a consistency error (cause 8)
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
    // tracker's feed-state changes for it.
    void on_message(const R::EventRecord* records, std::size_t n_records, const tracker::Transition* transitions,
                    std::size_t n_transitions) noexcept {
        const R::EventRecord* closer = nullptr;
        // An unappliable message (DESIGN.md 3.6.4) changes no level; its record
        // still closes an event like any other.
        const bool apply_it = !tracker_.unappliable();
        for (std::size_t i = 0; i < n_records; ++i) {
            if (apply_it ? apply(records[i]) : closes(records[i])) closer = &records[i];
        }
        sync(transitions, n_transitions, n_records ? &records[0] : nullptr);
        if (closer != nullptr) close(*closer);
    }

    // Feed-state changes with no message of their own (a loss, a silence).
    void on_transitions(const tracker::Transition* t, std::size_t n) noexcept { sync(t, n, nullptr); }

    // The instrument's book state (DESIGN.md 3.6.4): STALE when its feed
    // state is not clean or a consistency fault happened since its last clear.
    bool stale(std::size_t slot) const noexcept { return tracker_.state(slot).stale || faulted_[slot]; }

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

    // A record that closes the event without being applied (an unappliable
    // message): EndOfEvent, and not a reset.
    static bool closes(const R::EventRecord& r) noexcept {
        return (r.flags & R::flag::END_OF_EVENT) != 0 && r.kind != R::kind::SEQ_RESET &&
               r.kind != R::kind::CLEAR_CHANNEL;
    }

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
                    if (faulted_[slot]) {   // a ChannelReset clears a fault as it clears the feed state
                        faulted_[slot] = false;
                        mark(slot, Change::clear);
                    }
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
            } else if (r.kind != R::kind::CLEAR_BOOK && r.side == R::side::NONE) {
                // No side: unappliable (DESIGN.md 3.10), the tracker marked it STALE. No level changes.
            } else {
                const bool stale = this->stale(slot);
                const Side side = r.side == R::side::OFFER ? Side::offer : Side::bid;
                const std::uint64_t errors = book_.counters().consistency_errors;
                switch (r.kind) {
                    case R::kind::ADD: book_.add(slot, side, r.price, r.qty, stale); break;
                    case R::kind::MODIFY:
                        book_.change(slot, side, r.price, r.qty, static_cast<std::int64_t>(r.aux), stale);
                        break;
                    case R::kind::DELETE: book_.remove(slot, side, r.price, r.qty, stale); break;
                    case R::kind::CLEAR_SIDE: book_.clear_side(slot, side); break;
                    default: book_.clear(slot); break;   // CLEAR_BOOK
                }
                // A consistency error while CLEAN: the book goes STALE (cause 8);
                // the feed state, and so port 1, is untouched.
                if (book_.counters().consistency_errors != errors) mark(slot, Change::fault);
                if (r.kind == R::kind::CLEAR_BOOK && faulted_[slot]) {
                    faulted_[slot] = false;   // EmptyBook clears a fault as it clears the feed state
                    mark(slot, Change::clear);
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
            const bool stale = this->stale(slot);
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

    // What else changed a book state in this message, beside the tracker.
    enum class Change : std::uint8_t { none, fault, clear };
    void mark(std::size_t slot, Change c) noexcept {
        if (c == Change::fault) faulted_[slot] = true;
        if (change_[slot] == Change::none) extra_[n_extra_++] = static_cast<std::uint8_t>(slot);
        change_[slot] = c;
    }

    // Writes a STATE_CHANGE for every instrument whose book state changed, in
    // table slot order: one the tracker changed takes the tracker's cause and
    // identity; a fault (cause 8) or a clear of a faulted-only book (the
    // record's EmptyBook or ChannelReset) takes the message's (rec).
    void sync(const tracker::Transition* t, std::size_t n, const R::EventRecord* rec) noexcept {
        std::size_t ti = 0, ei = 0;
        // extra_ is in mark order; slots are few: sort it (insertion sort).
        for (std::size_t i = 1; i < n_extra_; ++i) {
            for (std::size_t j = i; j > 0 && extra_[j - 1] > extra_[j]; --j) std::swap(extra_[j - 1], extra_[j]);
        }
        while (ti < n || ei < n_extra_) {
            const std::size_t ts = ti < n ? t[ti].slot : kMaxInstruments;
            const std::size_t es = ei < n_extra_ ? extra_[ei] : kMaxInstruments;
            const std::size_t slot = ts <= es ? ts : es;
            const tracker::Transition* tr = ts == slot ? &t[ti] : nullptr;
            const Change c = es == slot ? change_[slot] : Change::none;
            if (ts == slot) ++ti;
            if (es == slot) {
                change_[slot] = Change::none;
                ++ei;
            }
            const bool now = stale(slot);
            if (now == book_stale_[slot]) continue;
            book_stale_[slot] = now;
            R::BookRecord r{};
            if (tr != nullptr) {
                identity(r, tr->at.seq_num, tr->at.version, tr->at.channel, tr->msg_index, tr->exch_time);
                r.header.cause = tr->cause;
            } else {
                identity(r, rec->seq_num, rec->seq_version, rec->channel, rec->msg_index, rec->exch_time);
                r.header.cause = c == Change::fault ? R::cause::CONSISTENCY_FAULT
                                 : rec->kind == R::kind::CLEAR_CHANNEL ? R::cause::CHANNEL_RESET
                                                                       : R::cause::EMPTY_BOOK;
                if (c == Change::fault) ++counters_.consistency_faults;
            }
            r.header.instrument_id = table_[slot].security_id;
            r.header.record_type = R::record_type::STATE_CHANGE;
            r.header.state = now ? R::book_state::STALE : R::book_state::CLEAN;
            if (!now) levels(r, slot);
            depth_flags(r, slot);
            ++counters_.state_changes;
            emit(r);
        }
        n_extra_ = 0;
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
    const tracker::Tracker& tracker_;   // the feed state
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
    // Book state (DESIGN.md 3.6.4): faulted since the last clear, the state
    // port 2 last wrote, and this message's other changes.
    std::array<bool, kMaxInstruments> faulted_{};
    std::array<bool, kMaxInstruments> book_stale_{};
    std::array<Change, kMaxInstruments> change_{};
    std::array<std::uint8_t, kMaxInstruments> extra_{};
    std::size_t n_extra_ = 0;
    BookSinkCounters counters_;
};

}  // namespace b3::book
