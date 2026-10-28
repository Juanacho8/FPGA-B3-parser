// book_capped.hpp: the capped price-level book, the RTL's algorithm.
//
// What it does:
//   Implements the depth-limited sorted levels of docs/CPP_BASELINE_CONTEXT.md
//   section 5 and docs/DESIGN.md 4.2, slot for slot. Per table instrument
//   and side: up to K slots (price, qty, order_count) sorted best first, a
//   valid count, a truncated flag and the boundary price B.
//     Invariant: the slots hold every level at or better than B; if not
//     truncated, they hold the whole side.
//     NEW at p: the level exists: qty += size, count += 1. Otherwise, if not
//       truncated or p is at or better than B, insert in sorted position; at
//       K + 1 levels the worst is dropped, truncated is set and B becomes the
//       new worst slot. Truncated and p worse than B: ignored.
//     CHANGE, DELETE: applied to the slot if the level is there (a DELETE
//       removes the slot at count 0, B unchanged). Worse than B while
//       truncated: ignored. At or better than B (or not truncated) and not in
//       the slots: consistency error.
//     Clears: no slots, truncated false.
//     Depth incomplete: truncated and fewer than N valid slots.
//   Same interface as book_uncapped.hpp, so book_sink.hpp takes either.
//   Statistics for choosing K (brief section 5, "Measuring K"): per side,
//   truncations, levels dropped, updates ignored, depth-incomplete episodes
//   and their duration in capture time, most valid slots.
//
// Layout (how it sits in block RAM): one fixed array of SideState, two per
// instrument (bid, offer), allocated once at construction. A SideState is one
// wide word: K slots plus valid, truncated and B. The RTL reads the word,
// compares the incoming price against all K slots at once, and writes it back
// shifted; every loop below runs over all K slots, as one comparator or one
// multiplexer per slot, with no early exit that depends on the data.
//
// What it must not do:
//   Allocate after construction, use a binary search, memmove or anything
//   else without an RTL equivalent, or know about port 1 or port 2.

#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>

#include "b3/book_params.hpp"
#include "b3/book_uncapped.hpp"
#include "b3/records.hpp"

namespace b3::book {

struct CappedSideStats {
    std::uint64_t truncations = 0;           // truncated went false -> true
    std::uint64_t levels_dropped = 0;        // levels pushed out past slot K
    std::uint64_t ignored = 0;               // updates worse than B while truncated
    std::uint64_t depth_incomplete = 0;      // episodes: depth incomplete went false -> true
    std::uint64_t depth_incomplete_ns = 0;   // total capture time spent depth incomplete
    std::uint64_t depth_incomplete_max_ns = 0;   // the longest episode
    std::uint64_t max_valid = 0;             // most valid slots seen
};

template <std::size_t K = kTrackedDepth, std::size_t N = kPublishedDepth, std::size_t Instruments = kMaxInstruments>
class CappedBook {
    static_assert(N <= K, "PUBLISHED_DEPTH (N) must not exceed TRACKED_DEPTH (K)");
    static_assert(K < 256, "valid is one byte");

public:
    static constexpr std::int64_t kNullPrice = std::numeric_limits<std::int64_t>::min();
    static constexpr std::size_t kSlots = K;

    struct SideState {
        std::array<BookLevel, K> slot{};   // best first; slots >= valid are don't-care
        std::int64_t boundary = 0;         // B, meaningful while truncated
        std::uint8_t valid = 0;
        bool truncated = false;
    };

    CappedBook() : sides_(std::make_unique<std::array<SideState, Instruments * 2>>()) {}

    std::size_t state_bytes() const noexcept { return sizeof(std::array<SideState, Instruments * 2>); }
    const BookCounters& counters() const noexcept { return counters_; }
    const CappedSideStats& stats(Side side) const noexcept { return stats_[static_cast<std::size_t>(side)]; }

    // Capture time of the datagram being processed: depth-incomplete durations.
    void set_time(std::uint64_t t) noexcept { now_ = t; }
    // End of the run: closes the episodes still open at time t.
    void finish(std::uint64_t t) noexcept {
        for (std::size_t i = 0; i < Instruments * 2; ++i) {
            if (incomplete_[i]) close_episode(i, t);
        }
    }

    void add(std::size_t slot, Side side, std::int64_t price, std::int64_t qty, bool stale) noexcept {
        if (price == kNullPrice) {
            ++counters_.null_price_orders;
            return;
        }
        SideState& s = at(slot, side);
        const Match m = match(s, side, price);
        if (m.found) {
            BookLevel& lv = s.slot[m.index];
            lv.qty += qty;
            lv.order_count += 1;
            if (lv.qty < 0) error(stale);
        } else if (!s.truncated || !worse(side, price, s.boundary)) {
            insert(s, side, m.better, BookLevel{price, qty, 1, 0});
            if (qty < 0) error(stale);
        } else {
            ++stats_[static_cast<std::size_t>(side)].ignored;
        }
        note(slot, side);
    }

    void change(std::size_t slot, Side side, std::int64_t price, std::int64_t qty, std::int64_t prev,
                bool stale) noexcept {
        if (price == kNullPrice) {
            ++counters_.null_price_orders;
            return;
        }
        SideState& s = at(slot, side);
        const Match m = match(s, side, price);
        if (!m.found) {
            missing(s, side, price, stale);
            return;
        }
        if (prev == kNullPrice) {
            error(stale);   // no previous size to apply
            return;
        }
        BookLevel& lv = s.slot[m.index];
        lv.qty += qty - prev;
        if (lv.qty < 0) error(stale);
    }

    void remove(std::size_t slot, Side side, std::int64_t price, std::int64_t qty, bool stale) noexcept {
        if (price == kNullPrice) {
            ++counters_.null_price_orders;
            return;
        }
        SideState& s = at(slot, side);
        const Match m = match(s, side, price);
        if (!m.found) {
            missing(s, side, price, stale);
            return;
        }
        BookLevel& lv = s.slot[m.index];
        lv.qty -= qty;
        if (lv.order_count == 0) {
            error(stale);
            return;
        }
        lv.order_count -= 1;
        if (lv.order_count == 0) {
            if (lv.qty != 0) error(stale);   // count 0 with quantity left
            erase(s, m.index);               // B unchanged
            note(slot, side);
            return;
        }
        if (lv.qty < 0) error(stale);
    }

    void clear_side(std::size_t slot, Side side) noexcept {
        SideState& s = at(slot, side);
        s.valid = 0;            // RTL: reset valid and truncated; the slots become don't-care
        s.truncated = false;
        s.boundary = 0;
        note(slot, side);
    }
    void clear(std::size_t slot) noexcept {
        clear_side(slot, Side::bid);
        clear_side(slot, Side::offer);
    }

    // The first min(n, valid) slots, best first. Returns how many.
    std::size_t top(std::size_t slot, Side side, BookLevel* out, std::size_t n) const noexcept {
        const SideState& s = at(slot, side);
        const std::size_t k = n < s.valid ? n : s.valid;
        for (std::size_t i = 0; i < k; ++i) out[i] = s.slot[i];
        return k;
    }
    std::size_t depth(std::size_t slot, Side side) const noexcept { return at(slot, side).valid; }
    bool truncated(std::size_t slot, Side side) const noexcept { return at(slot, side).truncated; }
    std::int64_t boundary(std::size_t slot, Side side) const noexcept { return at(slot, side).boundary; }
    bool depth_incomplete(std::size_t slot, Side side) const noexcept {
        const SideState& s = at(slot, side);
        return s.truncated && s.valid < N;
    }

private:
    struct Match {
        bool found = false;
        std::size_t index = 0;    // the slot holding the price, if found
        std::size_t better = 0;   // valid slots better than the price: the insert position
    };

    std::size_t index(std::size_t slot, Side side) const noexcept {
        return slot * 2 + static_cast<std::size_t>(side);
    }
    SideState& at(std::size_t slot, Side side) noexcept { return (*sides_)[index(slot, side)]; }
    const SideState& at(std::size_t slot, Side side) const noexcept { return (*sides_)[index(slot, side)]; }

    // a is better than b: a higher bid, a lower offer.
    static bool better(Side side, std::int64_t a, std::int64_t b) noexcept {
        return side == Side::bid ? a > b : a < b;
    }
    static bool worse(Side side, std::int64_t a, std::int64_t b) noexcept { return better(side, b, a); }

    // RTL: K comparators in parallel, one per slot, each giving "equal" and
    // "better than p" for a valid slot. The found index is a one-hot encoder
    // over the equal bits; the insert position is the population count of
    // the better bits (they are contiguous from slot 0, the slots being
    // sorted).
    static Match match(const SideState& s, Side side, std::int64_t price) noexcept {
        Match m;
        for (std::size_t i = 0; i < K; ++i) {
            const bool valid = i < s.valid;
            const bool equal = valid && s.slot[i].price == price;
            if (equal) {
                m.found = true;
                m.index = i;
            }
            m.better += (valid && better(side, s.slot[i].price, price)) ? 1 : 0;
        }
        return m;
    }

    // RTL: one multiplexer per slot. Slot i takes the new level if i == pos,
    // its left neighbour if i > pos, and keeps its value if i < pos; the old
    // slot K - 1 falls off the end when the side was full.
    void insert(SideState& s, Side side, std::size_t pos, const BookLevel& lv) noexcept {
        CappedSideStats& st = stats_[static_cast<std::size_t>(side)];
        const bool full = s.valid == K;
        if (full && pos == K) {
            // K + 1 levels with the new one the worst: it is the one dropped.
            drop(s, st, s.slot[K - 1].price);
            return;
        }
        for (std::size_t i = K; i-- > 1;) {
            if (i > pos) s.slot[i] = s.slot[i - 1];
        }
        s.slot[pos] = lv;
        if (full) {
            drop(s, st, s.slot[K - 1].price);   // the old worst was shifted out
        } else {
            ++s.valid;
            if (s.valid > st.max_valid) st.max_valid = s.valid;
        }
    }
    static void drop(SideState& s, CappedSideStats& st, std::int64_t new_boundary) noexcept {
        ++st.levels_dropped;
        if (!s.truncated) ++st.truncations;
        s.truncated = true;
        s.boundary = new_boundary;
        if (K > st.max_valid) st.max_valid = K;
    }

    // RTL: one multiplexer per slot. Slot i takes its right neighbour if
    // i >= pos; the last valid slot becomes don't-care.
    static void erase(SideState& s, std::size_t pos) noexcept {
        for (std::size_t i = 0; i + 1 < K; ++i) {
            if (i >= pos) s.slot[i] = s.slot[i + 1];
        }
        --s.valid;
    }

    // A CHANGE or DELETE whose level is not in the slots.
    void missing(const SideState& s, Side side, std::int64_t price, bool stale) noexcept {
        if (s.truncated && worse(side, price, s.boundary)) {
            ++stats_[static_cast<std::size_t>(side)].ignored;   // the untracked region
        } else {
            error(stale);
        }
    }

    // After a change to a side: depth-incomplete episodes.
    void note(std::size_t slot, Side side) noexcept {
        const std::size_t i = index(slot, side);
        const bool now = depth_incomplete(slot, side);
        if (now && !incomplete_[i]) {
            incomplete_[i] = true;
            since_[i] = now_;
            ++stats_[static_cast<std::size_t>(side)].depth_incomplete;
        } else if (!now && incomplete_[i]) {
            close_episode(i, now_);
        }
    }
    void close_episode(std::size_t i, std::uint64_t t) noexcept {
        incomplete_[i] = false;
        CappedSideStats& st = stats_[i % 2];
        const std::uint64_t d = t - since_[i];
        st.depth_incomplete_ns += d;
        if (d > st.depth_incomplete_max_ns) st.depth_incomplete_max_ns = d;
    }

    void error(bool stale) noexcept {
        if (stale) ++counters_.consistency_errors_stale;
        else ++counters_.consistency_errors;
    }

    std::unique_ptr<std::array<SideState, Instruments * 2>> sides_;
    // Statistics, outside the RTL's state.
    std::array<bool, Instruments * 2> incomplete_{};
    std::array<std::uint64_t, Instruments * 2> since_{};
    std::array<CappedSideStats, 2> stats_{};
    std::uint64_t now_ = 0;
    BookCounters counters_;
};

}  // namespace b3::book
