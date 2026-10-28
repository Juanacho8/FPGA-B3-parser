// book_uncapped.hpp: the uncapped price-level book (software only).
//
// What it does:
//   Per table instrument and side, a sorted flat array of levels (price,
//   qty, order_count) with the best price at the end, so the busy end of the
//   book moves the fewest bytes. All storage is one block reserved at
//   startup: instruments x 2 sides x a capacity per side (a config value).
//   Updates follow docs/DESIGN.md 4.5: add (qty += size, count += 1),
//   change (qty += new size - previous size), remove (qty -= size,
//   count -= 1, the level goes at count 0), and the clears. Trades never get
//   here. A new level past the capacity is counted and dropped. Null-price
//   orders are counted and kept out of the levels. Consistency errors are
//   counted, split by whether the instrument was CLEAN or STALE.
//
// What it must not do:
//   Allocate after construction, keep per-order state, or know about port 1
//   or port 2 (book_sink.hpp maps records to these calls).

#pragma once

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <vector>

#include "b3/records.hpp"

namespace b3::book {

using records::BookLevel;

enum class Side : std::uint8_t { bid = 0, offer = 1 };

struct BookCounters {
    std::uint64_t level_overflow = 0;          // new levels dropped: the side was full
    std::uint64_t consistency_errors = 0;      // while CLEAN: must be 0
    std::uint64_t consistency_errors_stale = 0;  // while STALE: expected after a loss
    std::uint64_t null_price_orders = 0;       // ADD/MODIFY/DELETE at a null price, outside the levels
};

class UncappedBook {
public:
    static constexpr std::int64_t kNullPrice = std::numeric_limits<std::int64_t>::min();

    UncappedBook(std::size_t instruments, std::size_t levels_per_side)
        : cap_(levels_per_side), size_(instruments * 2, 0), levels_(instruments * 2 * levels_per_side) {}

    std::size_t state_bytes() const noexcept {
        return levels_.size() * sizeof(BookLevel) + size_.size() * sizeof(std::size_t);
    }
    std::size_t levels_per_side() const noexcept { return cap_; }
    const BookCounters& counters() const noexcept { return counters_; }

    void add(std::size_t slot, Side side, std::int64_t price, std::int64_t qty, bool stale) noexcept {
        if (price == kNullPrice) {
            ++counters_.null_price_orders;
            return;
        }
        BookLevel* lv = find_or_insert(slot, side, price);
        if (lv == nullptr) return;   // overflow, counted
        lv->qty += qty;
        lv->order_count += 1;
        check(*lv, stale);
    }

    void change(std::size_t slot, Side side, std::int64_t price, std::int64_t qty, std::int64_t prev,
                bool stale) noexcept {
        if (price == kNullPrice) {
            ++counters_.null_price_orders;
            return;
        }
        BookLevel* lv = find(slot, side, price);
        if (lv == nullptr || prev == kNullPrice) {
            error(stale);            // no such level, or no previous size to apply
            return;
        }
        lv->qty += qty - prev;
        check(*lv, stale);
    }

    void remove(std::size_t slot, Side side, std::int64_t price, std::int64_t qty, bool stale) noexcept {
        if (price == kNullPrice) {
            ++counters_.null_price_orders;
            return;
        }
        BookLevel* lv = find(slot, side, price);
        if (lv == nullptr) {
            error(stale);
            return;
        }
        lv->qty -= qty;
        if (lv->order_count == 0) {
            error(stale);
            return;
        }
        lv->order_count -= 1;
        if (lv->order_count == 0) {
            if (lv->qty != 0) error(stale);   // count 0 with quantity left
            erase(slot, side, lv);
            return;
        }
        check(*lv, stale);
    }

    void clear_side(std::size_t slot, Side side) noexcept { size_[index(slot, side)] = 0; }
    void clear(std::size_t slot) noexcept {
        clear_side(slot, Side::bid);
        clear_side(slot, Side::offer);
    }

    // Up to n levels of one side, best first. Returns how many.
    std::size_t top(std::size_t slot, Side side, BookLevel* out, std::size_t n) const noexcept {
        const std::size_t count = size_[index(slot, side)];
        const BookLevel* base = &levels_[index(slot, side) * cap_];
        const std::size_t k = std::min(n, count);
        for (std::size_t i = 0; i < k; ++i) out[i] = base[count - 1 - i];   // best at the end
        return k;
    }
    std::size_t depth(std::size_t slot, Side side) const noexcept { return size_[index(slot, side)]; }

private:
    std::size_t index(std::size_t slot, Side side) const noexcept {
        return slot * 2 + static_cast<std::size_t>(side);
    }
    // Sorted so the best is last: bids by price ascending, offers descending.
    static bool before(Side side, std::int64_t a, std::int64_t b) noexcept {
        return side == Side::bid ? a < b : a > b;
    }
    BookLevel* lower_bound(std::size_t slot, Side side, std::int64_t price) noexcept {
        BookLevel* base = &levels_[index(slot, side) * cap_];
        BookLevel* end = base + size_[index(slot, side)];
        return std::lower_bound(base, end, price,
                                [side](const BookLevel& l, std::int64_t p) { return before(side, l.price, p); });
    }
    BookLevel* find(std::size_t slot, Side side, std::int64_t price) noexcept {
        BookLevel* it = lower_bound(slot, side, price);
        BookLevel* end = &levels_[index(slot, side) * cap_] + size_[index(slot, side)];
        return it != end && it->price == price ? it : nullptr;
    }
    BookLevel* find_or_insert(std::size_t slot, Side side, std::int64_t price) noexcept {
        BookLevel* it = lower_bound(slot, side, price);
        std::size_t& n = size_[index(slot, side)];
        BookLevel* base = &levels_[index(slot, side) * cap_];
        BookLevel* end = base + n;
        if (it != end && it->price == price) return it;
        if (n == cap_) {
            ++counters_.level_overflow;
            return nullptr;
        }
        std::memmove(it + 1, it, static_cast<std::size_t>(end - it) * sizeof(BookLevel));
        *it = BookLevel{price, 0, 0, 0};
        ++n;
        return it;
    }
    void erase(std::size_t slot, Side side, BookLevel* it) noexcept {
        std::size_t& n = size_[index(slot, side)];
        BookLevel* end = &levels_[index(slot, side) * cap_] + n;
        std::memmove(it, it + 1, static_cast<std::size_t>(end - it - 1) * sizeof(BookLevel));
        --n;
    }
    void check(const BookLevel& lv, bool stale) noexcept {
        if (lv.qty < 0) error(stale);
    }
    void error(bool stale) noexcept {
        if (stale) ++counters_.consistency_errors_stale;
        else ++counters_.consistency_errors;
    }

    std::size_t cap_;
    std::vector<std::size_t> size_;
    std::vector<BookLevel> levels_;
    BookCounters counters_;
};

}  // namespace b3::book
