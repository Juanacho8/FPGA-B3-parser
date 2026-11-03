"""b3books: the price-level books of the answer keys, and applying port 1 records to them.

What it does:
  A second implementation, in Python, of the C++ price-level books
  (DESIGN.md 4.2, 4.5): LevelBook (uncapped) and CappedBook (K slots per
  side, the RTL's algorithm), each with its per-side null-price bucket.
  apply() applies one port 1 record to one table instrument's book, the way
  cpp_baseline/include/b3/book_sink.hpp does, and says whether it found a
  consistency error while the instrument was CLEAN: the replay then marks
  the instrument STALE (b3replay.Replay.book_fault, cause 8). BookFeed runs
  a book beside a replay for the keys that need only its faults (the port 1
  converter and the counters key): after a consistency fault, port 1's bit 3
  and the tracker's counters follow the book (DESIGN.md 3.6.4).

What it must not do:
  Import b3gen, or read any C++ output.
"""

import b3contract as c

NULL_PRICE = -(1 << 63)
DEFAULT_LEVELS = 1024
BID, OFFER = 0, 1


def wrap(x):
    """int64 two's-complement wrap, as the C++ books (records.hpp wrap_add) and the RTL's adders."""
    return (x + (1 << 63)) % (1 << 64) - (1 << 63)


class NullBuckets:
    """The per-side null-price buckets of a price-level book (DESIGN.md 4.5):
    [qty, count] of the orders at a null price (market on auction), with a
    level's consistency rules. Not published on port 2 in v1."""

    def _bucket(self, slot, side):
        return self.null.setdefault((slot, side), [0, 0])

    def _null(self, op, slot, side, qty, prev, stale):
        self.null_price += 1
        b = self._bucket(slot, side)
        if op == "add":
            b[0] = wrap(b[0] + qty)
            b[1] += 1
            bad = b[0] < 0
        elif b[1] == 0:
            bad = True                    # a change or delete with no order at a null price
        elif op == "change":
            b[0] = wrap(b[0] + qty - prev)
            bad = b[0] < 0
        else:
            b[0] = wrap(b[0] - qty)
            b[1] -= 1
            if b[1] == 0:
                bad, b[0] = b[0] != 0, 0
            else:
                bad = b[0] < 0
        if bad:
            self._error(stale)

    def null_bucket(self, slot, side):
        return tuple(self._bucket(slot, side))


class LevelBook(NullBuckets):
    """Price levels per (slot, side): {price: [qty, count]}. DESIGN.md 4.5."""

    def __init__(self, cap=DEFAULT_LEVELS):
        self.cap = cap
        self.sides = {}
        self.null = {}
        self.level_overflow = self.errors = self.errors_stale = self.null_price = 0
        self.max_depth = {BID: 0, OFFER: 0}

    def _side(self, slot, side):
        return self.sides.setdefault((slot, side), {})

    def _error(self, stale):
        if stale:
            self.errors_stale += 1
        else:
            self.errors += 1

    def add(self, slot, side, price, qty, stale):
        if price == NULL_PRICE:
            self._null("add", slot, side, qty, 0, stale)
            return
        levels = self._side(slot, side)
        if price not in levels:
            if len(levels) == self.cap:
                self.level_overflow += 1
                return
            levels[price] = [0, 0]
            self.max_depth[side] = max(self.max_depth[side], len(levels))
        lv = levels[price]
        lv[0] = wrap(lv[0] + qty)
        lv[1] += 1
        if lv[0] < 0:
            self._error(stale)

    def change(self, slot, side, price, qty, prev, stale):
        if prev == NULL_PRICE:
            return            # unappliable: the replay marks the instrument STALE (DESIGN.md 4.5)
        if price == NULL_PRICE:
            self._null("change", slot, side, qty, prev, stale)
            return
        lv = self._side(slot, side).get(price)
        if lv is None:
            self._error(stale)
            return
        lv[0] = wrap(lv[0] + qty - prev)
        if lv[0] < 0:
            self._error(stale)

    def remove(self, slot, side, price, qty, stale):
        if price == NULL_PRICE:       # a present null: a market order (an absent price never gets here)
            self._null("remove", slot, side, qty, 0, stale)
            return
        levels = self._side(slot, side)
        lv = levels.get(price)
        if lv is None:
            self._error(stale)
            return
        lv[0] = wrap(lv[0] - qty)
        if lv[1] == 0:
            self._error(stale)
            return
        lv[1] -= 1
        if lv[1] == 0:
            if lv[0] != 0:
                self._error(stale)
            del levels[price]
            return
        if lv[0] < 0:
            self._error(stale)

    def clear(self, slot, side=None):
        for s in ((BID, OFFER) if side is None else (side,)):
            self.sides[(slot, s)] = {}
            self.null.pop((slot, s), None)

    def top(self, slot, side, n):
        levels = self._side(slot, side)
        prices = sorted(levels, reverse=(side == BID))[:n]
        return [(p, levels[p][0], levels[p][1]) for p in prices]


class CappedBook(NullBuckets):
    """The capped book (brief section 5, DESIGN.md 4.2), a second
    implementation beside the C++ book_capped.hpp: per (slot, side) a
    best-first list of at most K [price, qty, count] levels, a truncated
    flag and the boundary price B. Kept simple (list scans and inserts), so
    it shares no code or structure with the C++'s slot shifting."""

    def __init__(self, k, n):
        self.k, self.n = k, n
        self.sides = {}
        self.null = {}
        self.errors = self.errors_stale = self.null_price = 0
        self.level_overflow = 0
        self.stats = {s: dict(truncations=0, levels_dropped=0, ignored=0, depth_incomplete=0,
                              depth_incomplete_ns=0, depth_incomplete_max_ns=0, max_valid=0)
                      for s in (BID, OFFER)}
        self.now = 0
        self.since = {}   # (slot, side) -> start of the open depth-incomplete episode

    def _side(self, slot, side):
        return self.sides.setdefault((slot, side), {"levels": [], "truncated": False, "b": 0})

    def _error(self, stale):
        if stale:
            self.errors_stale += 1
        else:
            self.errors += 1

    @staticmethod
    def _better(side, a, b):
        return a > b if side == BID else a < b

    def _untracked(self, st, side, price):
        """Worse than B while truncated: the region the slots do not know."""
        return st["truncated"] and self._better(side, st["b"], price)

    def set_time(self, t):
        self.now = t

    def depth_incomplete(self, slot, side):
        st = self._side(slot, side)
        return st["truncated"] and len(st["levels"]) < self.n

    def _note(self, slot, side):
        key = (slot, side)
        on = self.depth_incomplete(slot, side)
        if on and key not in self.since:
            self.since[key] = self.now
            self.stats[side]["depth_incomplete"] += 1
        elif not on and key in self.since:
            self._close(key, self.now)

    def _close(self, key, t):
        d = t - self.since.pop(key)
        st = self.stats[key[1]]
        st["depth_incomplete_ns"] += d
        st["depth_incomplete_max_ns"] = max(st["depth_incomplete_max_ns"], d)

    def finish(self, t):
        for key in list(self.since):
            self._close(key, t)

    def add(self, slot, side, price, qty, stale):
        if price == NULL_PRICE:
            self._null("add", slot, side, qty, 0, stale)
            return
        st = self._side(slot, side)
        levels = st["levels"]
        lv = next((x for x in levels if x[0] == price), None)
        if lv is not None:
            lv[1] = wrap(lv[1] + qty)
            lv[2] += 1
            if lv[1] < 0:
                self._error(stale)
        elif self._untracked(st, side, price):
            self.stats[side]["ignored"] += 1
        else:
            pos = sum(1 for x in levels if self._better(side, x[0], price))
            levels.insert(pos, [price, qty, 1])
            if qty < 0:
                self._error(stale)
            stats = self.stats[side]
            if len(levels) > self.k:
                levels.pop()                      # the worst of K + 1
                stats["levels_dropped"] += 1
                if not st["truncated"]:
                    stats["truncations"] += 1
                st["truncated"] = True
                st["b"] = levels[-1][0]
            stats["max_valid"] = max(stats["max_valid"], len(levels))
        self._note(slot, side)

    def _find(self, slot, side, price, stale):
        st = self._side(slot, side)
        lv = next((x for x in st["levels"] if x[0] == price), None)
        if lv is None:
            if self._untracked(st, side, price):
                self.stats[side]["ignored"] += 1
            else:
                self._error(stale)
        return st, lv

    def change(self, slot, side, price, qty, prev, stale):
        if prev == NULL_PRICE:
            return            # unappliable: the replay marks the instrument STALE
        if price == NULL_PRICE:
            self._null("change", slot, side, qty, prev, stale)
            return
        _, lv = self._find(slot, side, price, stale)
        if lv is None:
            return
        lv[1] = wrap(lv[1] + qty - prev)
        if lv[1] < 0:
            self._error(stale)

    def remove(self, slot, side, price, qty, stale):
        if price == NULL_PRICE:       # a present null: a market order
            self._null("remove", slot, side, qty, 0, stale)
            return
        st, lv = self._find(slot, side, price, stale)
        if lv is None:
            return
        lv[1] = wrap(lv[1] - qty)
        if lv[2] == 0:
            self._error(stale)
            return
        lv[2] -= 1
        if lv[2] == 0:
            if lv[1] != 0:
                self._error(stale)
            st["levels"].remove(lv)            # B unchanged
            self._note(slot, side)
            return
        if lv[1] < 0:
            self._error(stale)

    def clear(self, slot, side=None):
        for s in ((BID, OFFER) if side is None else (side,)):
            self.sides[(slot, s)] = {"levels": [], "truncated": False, "b": 0}
            self.null.pop((slot, s), None)
            self._note(slot, s)

    def top(self, slot, side, n):
        return [tuple(x) for x in self._side(slot, side)["levels"][:n]]


def make_book(k=None, n=c.DEFAULT_PUBLISHED_DEPTH, levels=DEFAULT_LEVELS):
    """The uncapped book (k None or 0) or the capped book with K = k."""
    return LevelBook(levels) if not k else CappedBook(k, n)


def apply(book, slot, e, stale):
    """Applies one book-changing port 1 record (ADD, MODIFY, DELETE,
    CLEAR_SIDE, CLEAR_BOOK) of a table instrument. True when it found a
    consistency error while CLEAN. A record with no side (except CLEAR_BOOK)
    changes nothing: unappliable (DESIGN.md 3.10)."""
    if e.kind != c.CLEAR_BOOK and e.side == c.SIDE_NONE:
        return False
    side = OFFER if e.side == c.SIDE_OFFER else BID
    errors = book.errors
    if e.kind == c.ADD:
        book.add(slot, side, e.price, e.qty, stale)
    elif e.kind == c.MODIFY:
        prev = e.aux - (1 << 64) if e.aux >= 1 << 63 else e.aux
        book.change(slot, side, e.price, e.qty, prev, stale)
    elif e.kind == c.DELETE:
        book.remove(slot, side, e.price, e.qty, stale)
    elif e.kind == c.CLEAR_SIDE:
        book.clear(slot, side)
    else:
        book.clear(slot)
    return book.errors != errors


BOOK_KINDS = (c.ADD, c.MODIFY, c.DELETE, c.CLEAR_SIDE, c.CLEAR_BOOK)


class BookFeed:
    """A price-level book beside a replay, for its consistency faults only.
    feed(row, events) after each message's port 1 records: an unappliable
    message is skipped, a ChannelReset clears its channel, and a fault while
    CLEAN calls replay.book_fault(slot, point, row)."""

    def __init__(self, table, replay, k=None, n=c.DEFAULT_PUBLISHED_DEPTH):
        self.table = table
        self.slot = {sid: i for i, (sid, _) in enumerate(table)}
        self.replay = replay
        self.book = make_book(k, n)

    def feed(self, row, events):
        if self.replay.last_unappliable:
            return
        for e in events:
            if e.kind == c.CLEAR_CHANNEL:
                for slot, (_, ch) in enumerate(self.table):
                    if ch == e.channel:
                        self.book.clear(slot)
            elif e.kind in BOOK_KINDS:
                slot = self.slot.get(e.instrument_id)
                if slot is not None:
                    stale = self.replay.state[slot]["stale"]
                    if apply(self.book, slot, e, stale):
                        self.replay.book_fault(slot, (e.channel, e.seq_version, e.seq_num), row)
