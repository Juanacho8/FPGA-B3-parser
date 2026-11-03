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
    * a small Python price-level book (tools/b3books.py: LevelBook, or CappedBook) applies them, and
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

import b3books               # noqa: E402
import b3contract as c       # noqa: E402
import b3feed                # noqa: E402
import b3intent              # noqa: E402
import b3replay              # noqa: E402
import intent_to_events      # noqa: E402
from b3books import (BID, DEFAULT_LEVELS, NULL_PRICE, OFFER, CappedBook, LevelBook,   # noqa: E402,F401
                     NullBuckets, wrap)

DEFAULT_INTERVAL = 100_000_000


def file_header(n, status_interval, max_instruments=c.DEFAULT_MAX_INSTRUMENTS, k=None):
    """book.bin's header: K (0 for the uncapped book), the STATUS interval at offset 24."""
    return c.book_header(n, k or 0, max_instruments, status_interval).pack()


class TransitionReplay(b3replay.Replay):
    """b3replay.Replay, observed: each CLEAN <-> STALE change goes to pending as
    (slot, to_stale, cause, row or None, point), row None for a loss or a
    silence (with its point). The cause is the step that made the change: a
    silence in advance(), a gap in sequence(), an unappliable message, an
    rptSeq jump inside a message, otherwise the discarded tail after a packet; a clear is the
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

    def book_fault(self, slot, point, row=None):
        self._step, self._row = c.CAUSE_CONSISTENCY_FAULT, row
        super().book_fault(slot, point, row)
        self._step = self._row = None

    def unappliable_message(self, slot, point, reason):
        self._step = c.CAUSE_UNAPPLIABLE
        super().unappliable_message(slot, point, reason)
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
        self._row = None
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
            elif e.kind != c.CLEAR_BOOK and e.side == c.SIDE_NONE:
                pass      # no side: unappliable (DESIGN.md 3.10), the replay marked it STALE
            else:
                if b3books.apply(self.book, slot, e, self.stale_now(slot)):
                    # A consistency error while CLEAN: STALE, cause 8 (DESIGN.md 3.6.4).
                    self.replay.book_fault(slot, (e.channel, e.seq_version, e.seq_num), self._row)
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

    def on_message(self, events, row=None):
        self._row = row           # the message, for a consistency fault's identity
        closer = None
        skip = self.replay.last_unappliable      # changes no level (DESIGN.md 3.6.4); still closes
        for e in events:
            closes = (e.flags & c.FLAG_END_OF_EVENT and e.kind not in (c.SEQ_RESET, c.CLEAR_CHANNEL)) if skip \
                else self.apply(e)
            if closes:
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
        port2.on_message(list(closer.process(row, replay)), row)
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
        replay.packet(pkt, on_row=lambda row: port2.on_message(list(closer.process(row, replay)), row))
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
