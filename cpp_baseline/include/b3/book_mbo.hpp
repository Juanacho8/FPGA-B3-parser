// book_mbo.hpp: the MBO (per-order) book, software only.
//
// What it does:
//   Per table instrument, the resting orders keyed by order_id (B3's
//   secondaryOrderID), each with side, price and remaining qty (spec/LAYOUTS.md
//   12.3: an order is (securityID, side, price, secondaryOrderID); the id is
//   unique and only grows, so (instrument, id) finds it and side and price
//   are checked). Built from port 1 records only (DESIGN.md 3), the input a
//   downstream consumer of port 1 has; never from the wire.
//     ADD inserts; MODIFY sets the qty; DELETE removes; CLEAR_SIDE,
//     CLEAR_BOOK and CLEAR_CHANNEL remove the affected orders. Trades never
//     get here.
//   Per-order consistency checks, counted by reason and split by the
//   instrument's state (CLEAN or STALE) as in book_uncapped.hpp:
//     ADD of an order_id already resting; MODIFY of a missing order, at
//     another side or price than stored, or whose previous qty (port 1 aux)
//     is not the stored qty; DELETE of a missing order, at another side or
//     price, or whose qty is not the stored remaining qty.
//   On a mismatch the message wins (the stored order takes its side, price
//   and qty), so the book follows the feed; while CLEAN there must be none.
//   Per-price totals (qty, order count) per side, best first, for the
//   check against the price-level books (mbo_check.hpp).
//
// Storage: an order pool and an open-addressing hash table (linear probing,
// backward-shift deletion, no tombstones) sized at startup; per instrument
// and side, a doubly linked list through the pool for the clears. Nothing is
// allocated after construction; an ADD past the pool's capacity is counted
// and dropped.
//
// Future entry points (not built; DESIGN.md 4.8):
//   * load_order(): snapshot recovery (step 12) would clear the instrument and
//     load each order of the snapshot (LAYOUTS.md 12.6, 12.8) with it, then
//     resume incremental updates after the snapshot's rptSeq.
//   * levels(..., worse_than): host refill of the capped book (open item G)
//     would read the aggregated levels beyond the capped side's boundary
//     price B and push them back into its free slots.
//
// What it must not do:
//   Run in a timed build, read the wire, or allocate after construction.

#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <string_view>
#include <vector>

#include "b3/book_uncapped.hpp"
#include "b3/records.hpp"

namespace b3::book {

enum class MboFault : std::uint8_t {
    add_duplicate,
    modify_missing,
    modify_side,
    modify_price,
    modify_prev_qty,
    delete_missing,
    delete_side,
    delete_price,
    delete_qty,
};
inline constexpr std::size_t kMboFaults = 9;
inline constexpr std::array<std::string_view, kMboFaults> kMboFaultNames = {
    "add_duplicate", "modify_missing", "modify_side",   "modify_price", "modify_prev_qty",
    "delete_missing", "delete_side",   "delete_price", "delete_qty"};

struct MboCounters {
    std::array<std::uint64_t, kMboFaults> faults{};         // while CLEAN: must be 0
    std::array<std::uint64_t, kMboFaults> faults_stale{};   // while STALE: expected after a loss
    std::uint64_t pool_overflow = 0;    // ADDs dropped: the pool was full
    std::uint64_t table_overflow = 0;   // never, the table has twice the pool's slots; kept as a guard
    std::uint64_t null_price_orders = 0;   // orders loaded at a null price: kept, outside the per-price totals
    std::uint64_t peak_orders = 0;      // most orders resting at once, all instruments
};

class MboBook {
public:
    static constexpr std::int64_t kNullPrice = std::numeric_limits<std::int64_t>::min();
    static constexpr std::uint32_t kNone = std::numeric_limits<std::uint32_t>::max();

    MboBook(std::size_t instruments, std::size_t order_capacity)
        : pool_(order_capacity), table_(table_size(order_capacity), kNone),
          heads_(instruments * 2, kNone), resting_(instruments, 0), peak_(instruments, 0),
          scratch_(order_capacity) {
        for (std::size_t i = 0; i < pool_.size(); ++i) pool_[i].next = i + 1 < pool_.size() ? std::uint32_t(i + 1) : kNone;
        free_ = pool_.empty() ? kNone : 0;
    }

    std::size_t state_bytes() const noexcept {
        return pool_.size() * sizeof(Order) + table_.size() * sizeof(std::uint32_t) +
               heads_.size() * sizeof(std::uint32_t) + resting_.size() * sizeof(std::uint64_t);
    }
    std::size_t capacity() const noexcept { return pool_.size(); }
    const MboCounters& counters() const noexcept { return counters_; }
    std::uint64_t resting(std::size_t slot) const noexcept { return resting_[slot]; }
    std::uint64_t peak(std::size_t slot) const noexcept { return peak_[slot]; }

    void add(std::size_t slot, Side side, std::int64_t price, std::int64_t qty, std::uint64_t id, bool stale) noexcept {
        const std::uint32_t at = find(slot, id);
        if (at != kNone) {
            fault(MboFault::add_duplicate, stale);
            restate(at, side, price, qty);
            return;
        }
        load_order(slot, side, price, qty, id);
    }

    void modify(std::size_t slot, Side side, std::int64_t price, std::int64_t qty, std::int64_t prev,
                std::uint64_t id, bool stale) noexcept {
        const std::uint32_t at = find(slot, id);
        if (at == kNone) {
            fault(MboFault::modify_missing, stale);
            return;
        }
        const Order& o = pool_[at];
        if (o.side != side) fault(MboFault::modify_side, stale);
        if (o.price != price) fault(MboFault::modify_price, stale);
        if (prev == kNullPrice || o.qty != prev) fault(MboFault::modify_prev_qty, stale);
        restate(at, side, price, qty);   // keeps its place: a CHANGE keeps priority
    }

    // price kNullPrice (DeleteOrder_MBO before schema v15 carries none): the
    // order is found by id and its stored price is not checked.
    void remove(std::size_t slot, Side side, std::int64_t price, std::int64_t qty, std::uint64_t id,
                bool stale) noexcept {
        const std::uint32_t at = find(slot, id);
        if (at == kNone) {
            fault(MboFault::delete_missing, stale);
            return;
        }
        const Order& o = pool_[at];
        if (o.side != side) fault(MboFault::delete_side, stale);
        if (price != kNullPrice && o.price != price) fault(MboFault::delete_price, stale);
        if (o.qty != qty) fault(MboFault::delete_qty, stale);
        erase(at);
    }

    void clear_side(std::size_t slot, Side side) noexcept {
        std::uint32_t& head = heads_[list(slot, side)];
        while (head != kNone) erase(head);
    }
    void clear(std::size_t slot) noexcept {
        clear_side(slot, Side::bid);
        clear_side(slot, Side::offer);
    }

    // Entry point for snapshot recovery (step 12; not built): insert one
    // order with no consistency check. ADD uses it after its duplicate check.
    void load_order(std::size_t slot, Side side, std::int64_t price, std::int64_t qty, std::uint64_t id) noexcept {
        if (free_ == kNone) {
            ++counters_.pool_overflow;
            return;
        }
        const std::uint32_t at = free_;
        if (!insert_key(slot, id, at)) {
            ++counters_.table_overflow;
            return;
        }
        free_ = pool_[at].next;
        Order& o = pool_[at];
        o = Order{id, price, qty, kNone, kNone, static_cast<std::uint16_t>(slot), side};
        link(at);
        if (price == kNullPrice) ++counters_.null_price_orders;
        ++in_use_;
        if (in_use_ > counters_.peak_orders) counters_.peak_orders = in_use_;
        if (++resting_[slot] > peak_[slot]) peak_[slot] = resting_[slot];
    }

    // The per-price totals of one side, best first, every level (n at most):
    // (price, total qty, order count). Null-price orders are left out, as the
    // price-level books keep them out. With worse_than set, only the levels
    // strictly worse than that price: what a host refill of the capped book
    // (open item G; not built) would push back below its boundary B.
    std::size_t levels(std::size_t slot, Side side, records::BookLevel* out, std::size_t n,
                       const std::int64_t* worse_than = nullptr) noexcept {
        std::size_t k = 0;
        for (std::uint32_t at = heads_[list(slot, side)]; at != kNone; at = pool_[at].next) {
            const Order& o = pool_[at];
            if (o.price == kNullPrice) continue;
            if (worse_than != nullptr && !(side == Side::bid ? o.price < *worse_than : o.price > *worse_than)) continue;
            scratch_[k++] = records::BookLevel{o.price, o.qty, 1, 0};
        }
        std::sort(scratch_.begin(), scratch_.begin() + static_cast<std::ptrdiff_t>(k),
                  [side](const records::BookLevel& a, const records::BookLevel& b) {
                      return side == Side::bid ? a.price > b.price : a.price < b.price;
                  });
        std::size_t m = 0;
        for (std::size_t i = 0; i < k; ++i) {
            if (m > 0 && out[m - 1].price == scratch_[i].price) {
                out[m - 1].qty += scratch_[i].qty;
                out[m - 1].order_count += 1;
            } else if (m < n) {
                out[m++] = scratch_[i];
            } else {
                break;
            }
        }
        return m;
    }

    // One resting order, for tests: true and its fields if (slot, id) rests.
    bool order(std::size_t slot, std::uint64_t id, Side& side, std::int64_t& price, std::int64_t& qty) const noexcept {
        const std::uint32_t at = find(slot, id);
        if (at == kNone) return false;
        side = pool_[at].side;
        price = pool_[at].price;
        qty = pool_[at].qty;
        return true;
    }
    // The orders of one side in list order (newest first), for tests.
    std::size_t ids(std::size_t slot, Side side, std::uint64_t* out, std::size_t n) const noexcept {
        std::size_t k = 0;
        for (std::uint32_t at = heads_[list(slot, side)]; at != kNone && k < n; at = pool_[at].next) {
            out[k++] = pool_[at].id;
        }
        return k;
    }

private:
    struct Order {
        std::uint64_t id = 0;
        std::int64_t price = 0;
        std::int64_t qty = 0;
        std::uint32_t prev = kNone;   // in the (slot, side) list; next doubles as the free list
        std::uint32_t next = kNone;
        std::uint16_t slot = 0;
        Side side = Side::bid;
    };
    static_assert(sizeof(Order) == 40);

    static std::size_t table_size(std::size_t capacity) noexcept {
        std::size_t n = 16;
        while (n < 2 * capacity) n *= 2;
        return n;
    }
    std::size_t list(std::size_t slot, Side side) const noexcept { return slot * 2 + static_cast<std::size_t>(side); }
    std::size_t home(std::size_t slot, std::uint64_t id) const noexcept {
        std::uint64_t h = id ^ (std::uint64_t{slot} << 56);   // splitmix64 finaliser
        h = (h ^ (h >> 30)) * 0xbf58476d1ce4e5b9ULL;
        h = (h ^ (h >> 27)) * 0x94d049bb133111ebULL;
        h ^= h >> 31;
        return static_cast<std::size_t>(h) & (table_.size() - 1);
    }

    std::uint32_t find(std::size_t slot, std::uint64_t id) const noexcept {
        for (std::size_t i = home(slot, id), probes = 0; probes < table_.size(); i = (i + 1) & (table_.size() - 1), ++probes) {
            const std::uint32_t at = table_[i];
            if (at == kNone) return kNone;
            if (pool_[at].id == id && pool_[at].slot == slot) return at;
        }
        return kNone;
    }
    bool insert_key(std::size_t slot, std::uint64_t id, std::uint32_t at) noexcept {
        for (std::size_t i = home(slot, id), probes = 0; probes < table_.size(); i = (i + 1) & (table_.size() - 1), ++probes) {
            if (table_[i] == kNone) {
                table_[i] = at;
                return true;
            }
        }
        return false;
    }
    // Backward-shift deletion: later entries of the probe run move up, so a
    // lookup never meets a hole before its key.
    void erase_key(std::uint32_t at) noexcept {
        const std::size_t mask = table_.size() - 1;
        std::size_t i = home(pool_[at].slot, pool_[at].id);
        while (table_[i] != at) i = (i + 1) & mask;
        for (std::size_t j = (i + 1) & mask; table_[j] != kNone; j = (j + 1) & mask) {
            const std::size_t h = home(pool_[table_[j]].slot, pool_[table_[j]].id);
            // Move j into the hole at i unless its home lies cyclically in (i, j].
            if (((j - h) & mask) >= ((j - i) & mask)) {
                table_[i] = table_[j];
                i = j;
            }
        }
        table_[i] = kNone;
    }

    void link(std::uint32_t at) noexcept {
        Order& o = pool_[at];
        std::uint32_t& head = heads_[list(o.slot, o.side)];
        o.prev = kNone;
        o.next = head;
        if (head != kNone) pool_[head].prev = at;
        head = at;
    }
    void unlink(std::uint32_t at) noexcept {
        Order& o = pool_[at];
        if (o.prev != kNone) pool_[o.prev].next = o.next;
        else heads_[list(o.slot, o.side)] = o.next;
        if (o.next != kNone) pool_[o.next].prev = o.prev;
    }
    void restate(std::uint32_t at, Side side, std::int64_t price, std::int64_t qty) noexcept {
        Order& o = pool_[at];
        if (o.side != side) {
            unlink(at);
            o.side = side;
            link(at);
        }
        o.price = price;
        o.qty = qty;
    }
    void erase(std::uint32_t at) noexcept {
        erase_key(at);
        unlink(at);
        Order& o = pool_[at];
        --resting_[o.slot];
        --in_use_;
        o.next = free_;
        free_ = at;
    }
    void fault(MboFault f, bool stale) noexcept {
        (stale ? counters_.faults_stale : counters_.faults)[static_cast<std::size_t>(f)] += 1;
    }

    std::vector<Order> pool_;
    std::vector<std::uint32_t> table_;
    std::vector<std::uint32_t> heads_;
    std::vector<std::uint64_t> resting_;
    std::vector<std::uint64_t> peak_;
    std::vector<records::BookLevel> scratch_;
    std::uint32_t free_ = kNone;
    std::uint64_t in_use_ = 0;
    MboCounters counters_;
};

}  // namespace b3::book
