#!/usr/bin/env python3
"""expected_book: the expected port 2 book.bin for one feed, without the C++.

What it does:
  An independent implementation of docs/DESIGN.md 4 (port 2), with the
  uncapped book or the capped book (K slots per side, the RTL's algorithm,
  DESIGN.md 4.2; --tracked-depth K). For one feed of a generator set:
    * the packet order and capture times come from the feed's pcap, read
      with the generator's independent reader (tools/b3feed.py); a datagram
      the view below does not hold (a duplicate, a late packet, a short
      datagram) only advances time (STATUS, the silence rule)
    * the messages are the feed's view of intent.jsonl (tools/b3intent.py)
    * port 1 records come from tools/intent_to_events.py's Closer and the
      stale state from tools/b3replay.py's Replay (observed through a
      subclass, never changed), the answer keys port 1 is checked against
    * a small Python price-level book (LevelBook, or CappedBook, below) applies them, and
      the emission rules of DESIGN.md 4.3 write BOOK_UPDATE, STATE_CHANGE
      and STATUS records with output sequence numbers
  The generator's tests/book_builder.py is not used: it asserts strict
  sequencing and in-order rptSeq and fails on a delete of an unknown order,
  so it cannot follow a feed with losses; this book is price-level, like
  the C++ and the RTL, and keeps no orders.
  It also records the books at given placements (the book-key check).

What it must not do:
  Import b3gen or read any C++ output.

Usage:
  python3 tools/expected_book.py data/scenarios/gap --feed A -o expected_book.bin [--tracked-depth 16]
"""

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "feed_generator" / "tests"))

import b3contract as c       # noqa: E402
import b3feed                # noqa: E402
import b3intent              # noqa: E402
import b3replay              # noqa: E402
import intent_to_events      # noqa: E402

NULL_PRICE = -(1 << 63)
DEFAULT_LEVELS = 1024
DEFAULT_INTERVAL = 100_000_000
BID, OFFER = 0, 1


def file_header(n, status_interval, max_instruments=c.DEFAULT_MAX_INSTRUMENTS, k=None):
    """book.bin's header: K (0 for the uncapped book), the STATUS interval at offset 24."""
    return c.book_header(n, k or 0, max_instruments, status_interval).pack()


class LevelBook:
    """Price levels per (slot, side): {price: [qty, count]}. DESIGN.md 4.5."""

    def __init__(self, cap=DEFAULT_LEVELS):
        self.cap = cap
        self.sides = {}
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
            self.null_price += 1
            return
        levels = self._side(slot, side)
        if price not in levels:
            if len(levels) == self.cap:
                self.level_overflow += 1
                return
            levels[price] = [0, 0]
            self.max_depth[side] = max(self.max_depth[side], len(levels))
        lv = levels[price]
        lv[0] += qty
        lv[1] += 1
        if lv[0] < 0:
            self._error(stale)

    def change(self, slot, side, price, qty, prev, stale):
        if price == NULL_PRICE:
            self.null_price += 1
            return
        lv = self._side(slot, side).get(price)
        if lv is None or prev == NULL_PRICE:
            self._error(stale)
            return
        lv[0] += qty - prev
        if lv[0] < 0:
            self._error(stale)

    def remove(self, slot, side, price, qty, stale):
        if price == NULL_PRICE:
            self.null_price += 1
            return
        levels = self._side(slot, side)
        lv = levels.get(price)
        if lv is None:
            self._error(stale)
            return
        lv[0] -= qty
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

    def top(self, slot, side, n):
        levels = self._side(slot, side)
        prices = sorted(levels, reverse=(side == BID))[:n]
        return [(p, levels[p][0], levels[p][1]) for p in prices]


class CappedBook:
    """The capped book (brief section 5, DESIGN.md 4.2), a second
    implementation beside the C++ book_capped.hpp: per (slot, side) a
    best-first list of at most K [price, qty, count] levels, a truncated
    flag and the boundary price B. Kept simple (list scans and inserts), so
    it shares no code or structure with the C++'s slot shifting."""

    def __init__(self, k, n):
        self.k, self.n = k, n
        self.sides = {}
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
            self.null_price += 1
            return
        st = self._side(slot, side)
        levels = st["levels"]
        lv = next((x for x in levels if x[0] == price), None)
        if lv is not None:
            lv[1] += qty
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
        if price == NULL_PRICE:
            self.null_price += 1
            return
        _, lv = self._find(slot, side, price, stale)
        if lv is None:
            return
        if prev == NULL_PRICE:
            self._error(stale)
            return
        lv[1] += qty - prev
        if lv[1] < 0:
            self._error(stale)

    def remove(self, slot, side, price, qty, stale):
        if price == NULL_PRICE:
            self.null_price += 1
            return
        st, lv = self._find(slot, side, price, stale)
        if lv is None:
            return
        lv[1] -= qty
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
            self._note(slot, s)

    def top(self, slot, side, n):
        return [tuple(x) for x in self._side(slot, side)["levels"][:n]]


class TransitionReplay(b3replay.Replay):
    """b3replay.Replay, observed: each CLEAN <-> STALE change goes to pending as
    (slot, to_stale, cause, row or None, point), row None for a loss or a
    silence (with its point). The cause is the step that made the change: a
    silence in advance(), a gap in sequence(), an rptSeq jump inside a
    message, otherwise the discarded tail after a packet; a clear is the
    message's EmptyBook or ChannelReset."""

    def __init__(self, table, manifest, feed, silence_ns=b3replay.DEFAULT_SILENCE_NS):
        super().__init__(table, manifest, feed, silence_ns)
        self.pending = []
        self._row = None
        self._step = None

    def advance(self, t):
        self._step = c.CAUSE_SILENCE
        super().advance(t)
        self._step = None

    def sequence(self, chan, ch, ver, num):
        self._step = c.CAUSE_GAP
        super().sequence(chan, ch, ver, num)
        self._step = None

    def stale(self, slot, point):
        was = self.state[slot]["stale"]
        super().stale(slot, point)
        if not was:
            cause = self._step or (c.CAUSE_RPT_SEQ_JUMP if self._row is not None else c.CAUSE_DISCARDED_TAIL)
            self.pending.append((slot, True, cause, self._row, point))

    def message(self, row, chan):
        self._row = row
        before = [st["stale"] for st in self.state]
        super().message(row, chan)
        cause = c.CAUSE_CHANNEL_RESET if row["templateId"] == b3replay.CHANNEL_RESET else c.CAUSE_EMPTY_BOOK
        for slot, (was, st) in enumerate(zip(before, self.state)):
            if was and not st["stale"]:
                self.pending.append((slot, False, cause, row, None))
        self._row = None


def time_field(row):
    t = row["templateId"]
    name = intent_to_events.TIME_FIELD.get(t)
    return intent_to_events.field(row, name) if name else 0


class Port2:
    """DESIGN.md 4.3 over port 1 records and state changes."""

    def __init__(self, table, replay, n=c.DEFAULT_PUBLISHED_DEPTH, levels=DEFAULT_LEVELS,
                 status_interval=DEFAULT_INTERVAL, publish_stale=False, k=None):
        self.table = table
        self.slot = {sid: i for i, (sid, _) in enumerate(table)}
        self.replay = replay
        self.n = n
        self.k = k            # None: the uncapped book; else the capped book with K slots
        self.book = LevelBook(levels) if k is None else CappedBook(k, n)
        self.interval = status_interval
        self.publish_stale = publish_stale
        self.records = []
        self.out_seq = 0
        self.next_tick = None
        self.capture = 0
        self.touched = {}     # channel -> {slot: recovery} in order of first touch
        self.counts = dict(book_updates=0, state_changes=0, statuses=0, untracked_records=0)

    def begin_datagram(self, t):
        """Time reaches t: one STATUS for the latest tick due, ticks_skipped
        (at offset 24) the earlier ones it stands for (DESIGN.md 4.3)."""
        if self.next_tick is None:
            self.next_tick = t + self.interval if self.interval else None
        if self.interval and t >= self.next_tick:
            due = (t - self.next_tick) // self.interval + 1
            latest = self.next_tick + (due - 1) * self.interval
            self.emit(c.STATUS, (0, 0, 0, 0), 0, due - 1, c.BOOK_CLEAN, 0, capture=latest)
            self.counts["statuses"] += 1
            self.next_tick = latest + self.interval
        self.capture = t
        if self.k is not None:
            self.book.set_time(t)

    def depth_flags(self, slot):
        if self.k is None:
            return 0
        return ((c.BOOK_FLAG_DEPTH_INCOMPLETE_BID if self.book.depth_incomplete(slot, BID) else 0) |
                (c.BOOK_FLAG_DEPTH_INCOMPLETE_OFFER if self.book.depth_incomplete(slot, OFFER) else 0))

    def emit(self, rtype, ident, instrument, exch_time, state, flags, slot=None, capture=None, cause=0):
        self.out_seq += 1
        seq_num, version, channel, index = ident
        bids = self.book.top(slot, BID, self.n) if slot is not None else []
        offers = self.book.top(slot, OFFER, self.n) if slot is not None else []
        rec = c.BookRecord(self.out_seq, rtype, seq_num, version, channel, index, instrument, exch_time,
                           self.capture if capture is None else capture, state, flags, cause,
                           tuple(bids), tuple(offers))
        self.records.append(rec.pack(self.n))

    def stale_now(self, slot):
        return self.replay.state[slot]["stale"]

    def apply(self, e):
        touched = self.touched.setdefault(e.channel, {})
        if e.kind in (c.SEQ_RESET, c.CLEAR_CHANNEL):
            touched.clear()
            if e.kind == c.CLEAR_CHANNEL:
                for slot, (_, ch) in enumerate(self.table):
                    if ch == e.channel:
                        self.book.clear(slot)
                        touched[slot] = touched.get(slot, False) or bool(e.flags & c.FLAG_RECOVERY)
            return False
        if e.kind in (c.ADD, c.MODIFY, c.DELETE, c.CLEAR_SIDE, c.CLEAR_BOOK):
            slot = self.slot.get(e.instrument_id)
            if slot is None:
                self.counts["untracked_records"] += 1
            else:
                stale = self.stale_now(slot)
                side = OFFER if e.side == c.SIDE_OFFER else BID
                if e.kind == c.ADD:
                    self.book.add(slot, side, e.price, e.qty, stale)
                elif e.kind == c.MODIFY:
                    prev = e.aux - (1 << 64) if e.aux >= 1 << 63 else e.aux
                    self.book.change(slot, side, e.price, e.qty, prev, stale)
                elif e.kind == c.DELETE:
                    self.book.remove(slot, side, e.price, e.qty, stale)
                elif e.kind == c.CLEAR_SIDE:
                    self.book.clear(slot, side)
                else:
                    self.book.clear(slot)
                touched[slot] = touched.get(slot, False) or bool(e.flags & c.FLAG_RECOVERY)
        return bool(e.flags & c.FLAG_END_OF_EVENT)

    def transitions(self):
        for slot, to_stale, cause, row, point in self.replay.pending:
            if row is None:
                ident, exch = (point[2], point[1], point[0], 0), 0
            else:
                ident = (row["sequenceNumber"], row["sequenceVersion"], row["channel"], row["index"])
                exch = time_field(row)
            sid = self.table[slot][0]
            if to_stale:
                self.emit(c.STATE_CHANGE, ident, sid, exch, c.BOOK_STALE, self.depth_flags(slot), cause=cause)
            else:
                self.emit(c.STATE_CHANGE, ident, sid, exch, c.BOOK_CLEAN, self.depth_flags(slot), slot=slot,
                          cause=cause)
            self.counts["state_changes"] += 1
        self.replay.pending.clear()

    def close(self, e):
        touched = self.touched.setdefault(e.channel, {})
        for slot, recovery in touched.items():
            stale = self.stale_now(slot)
            if stale and not self.publish_stale:
                continue
            self.emit(c.BOOK_UPDATE, (e.seq_num, e.seq_version, e.channel, e.msg_index), self.table[slot][0],
                      e.exch_time, c.BOOK_STALE if stale else c.BOOK_CLEAN,
                      (c.BOOK_FLAG_RECOVERY if recovery else 0) | self.depth_flags(slot), slot=slot)
            self.counts["book_updates"] += 1
        touched.clear()

    def on_message(self, events):
        closer = None
        for e in events:
            if self.apply(e):
                closer = e
        self.transitions()
        if closer is not None:
            self.close(closer)


def run(folder, feed, table=None, n=c.DEFAULT_PUBLISHED_DEPTH, levels=DEFAULT_LEVELS,
        status_interval=DEFAULT_INTERVAL, publish_stale=False, checkpoints=None,
        silence_ns=b3replay.DEFAULT_SILENCE_NS, k=None):
    """(Port2, snapshots) for one feed, with the uncapped book (k None) or
    the capped book with K = k; snapshots = {placement: {securityID: (state,
    depth flags, bids, offers)}} after each message whose placement is in
    checkpoints. Depth flags: 1 bid, 2 offer depth incomplete."""
    folder = Path(folder)
    intent = folder / "intent.jsonl"
    manifest = b3intent.load_manifest(intent)
    table = table if table is not None else b3replay.default_table()
    replay = TransitionReplay(table, manifest, feed, silence_ns)
    port2 = Port2(table, replay, n, levels, status_interval, publish_stale, k)
    closer = intent_to_events.Closer()
    snapshots = {}
    checkpoints = checkpoints or set()

    def on_row(row):
        port2.on_message(list(closer.process(row, replay)))
        key = b3intent.row_key(row)
        if key in checkpoints:
            snapshots[key] = {sid: ("STALE" if replay.state[slot]["stale"] else "CLEAN",
                                    port2.depth_flags(slot) >> 1,
                                    port2.book.top(slot, BID, n), port2.book.top(slot, OFFER, n))
                              for slot, (sid, _) in enumerate(table)}

    # The feed's own packet order and capture times (tools/b3feed.py): STATUS
    # first, then the silence rule's state changes, then the packet; a
    # discarded tail's loss after the packet.
    b3feed.drive(intent, manifest, feed, folder / f"feed{feed}.pcap", replay, on_row=on_row,
                 on_time=port2.begin_datagram, on_after_time=port2.transitions,
                 on_after_packet=port2.transitions)
    if k is not None:
        port2.book.finish(port2.capture)
    return port2, snapshots


def run_packets(packets, times, table, manifest, feed="A", n=c.DEFAULT_PUBLISHED_DEPTH,
                status_interval=DEFAULT_INTERVAL, publish_stale=False, k=None):
    """Port2 over hand-given packets (lists of intent-shaped rows) with
    their capture times, every packet delivered: tests and DESIGN.md 4.7."""
    replay = TransitionReplay(table, manifest, feed)
    port2 = Port2(table, replay, n, DEFAULT_LEVELS, status_interval, publish_stale, k)
    closer = intent_to_events.Closer()
    for pkt, t in zip(packets, times):
        port2.begin_datagram(t)          # the same steps as tools/b3feed.py's drive()
        replay.advance(t)
        port2.transitions()
        replay.saw(pkt[0]["channel"], t)
        replay.packet(pkt, on_row=lambda row: port2.on_message(list(closer.process(row, replay))))
        port2.transitions()
    if k is not None and times:
        port2.book.finish(times[-1])
    return port2


def book_bytes(port2, status_interval=DEFAULT_INTERVAL):
    return file_header(port2.n, status_interval, k=port2.k) + b"".join(port2.records)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("set", help="a generator set folder")
    ap.add_argument("--feed", default="A", choices=("A", "B"))
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--book-levels", type=int, default=DEFAULT_LEVELS, help="the uncapped book's levels per side")
    ap.add_argument("--published-depth", type=int, default=c.DEFAULT_PUBLISHED_DEPTH, help="N")
    ap.add_argument("--tracked-depth", type=int, default=0, help="K of the capped book; 0 (default) = uncapped")
    ap.add_argument("--status-interval-ns", type=int, default=DEFAULT_INTERVAL)
    ap.add_argument("--instruments", help="instrument table (default: the generator config's)")
    args = ap.parse_args(argv)
    table = b3replay.read_table(args.instruments) if args.instruments else None
    port2, _ = run(args.set, args.feed, table, n=args.published_depth, levels=args.book_levels,
                   status_interval=args.status_interval_ns, k=args.tracked_depth or None)
    Path(args.out).write_bytes(book_bytes(port2, args.status_interval_ns))
    print(f"{args.out}: {len(port2.records)} records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
