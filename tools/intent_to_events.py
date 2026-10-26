#!/usr/bin/env python3
"""intent_to_events: converts intent.jsonl into the expected events.bin.

What it does:
  An independent implementation of the port 1 contract (docs/DESIGN.md
  section 3), including the event-closing state machine (section 3.5).
  For one feed it writes exactly the records a correct decoder of that
  feed's pcap writes: rows the feed lost are left out, and so are the
  messages our malformed policy discards (tools/b3intent.py).

  Feed A only. Feed B carries the same packets per channel, but merges the
  channels in an order set by its own jittered timestamps, which the intent
  log does not record (see DESIGN.md section 3.7).

What it must not do:
  Import b3gen (its encoders or layouts), read the pcap, or look at C++
  output. Its only inputs are intent.jsonl and manifest.json.

Usage:
  python3 tools/intent_to_events.py data/small/intent.jsonl -o expected_events.bin
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import b3contract as c   # noqa: E402
import b3intent          # noqa: E402

# ---- 1. B3 facts used (spec/LAYOUTS.md) ----
END_OF_EVENT_BIT = 0x80   # matchEventIndicator bit 7
RECOVERY_BIT = 0x20       # bit 5
IMPLIED_BIT = 0x10        # bit 4

MD_NEW, MD_CHANGE, MD_DELETE_THRU = 0, 1, 3

# Templates with a matchEventIndicator field. Not: 1, 2, 12, 30, 55, 71 and
# unknown templates (55 never ends an event, LAYOUTS.md 12.4).
HAS_MEI = {3, 5, 9, 10, 11, 15, 16, 17, 19, 21, 22, 24, 25, 27, 28, 29,
           50, 51, 52, 53, 54, 56, 57}
SIDES = {"0": c.SIDE_BID, "1": c.SIDE_OFFER}

# Each template's time field, used for exch_time (DESIGN.md section 3.4).
TIME_FIELD = {
    3: "transactTime", 5: "origTime", 9: "mDEntryTimestamp", 10: "transactTime",
    11: "mDEntryTimestamp", 15: "mDEntryTimestamp", 16: "mDEntryTimestamp",
    17: "mDEntryTimestamp", 19: "mDEntryTimestamp", 21: "mDEntryTimestamp",
    22: "mDEntryTimestamp", 24: "mDEntryTimestamp", 25: "mDEntryTimestamp",
    27: "mDEntryTimestamp", 28: "mDEntryTimestamp", 29: "mDEntryTimestamp",
    50: "transactTime", 51: "transactTime", 52: "transactTime", 53: "transactTime",
    54: "transactTime", 55: "transactTime", 56: "mDEntryTimestamp", 57: "transactTime",
}

# Null value of every field a port 1 record reads that an older version can
# lack (all came in since v15/v16). An absent field is written as its null.
ABSENT_AS = {"transactTime": 0, "mDEntryPx": c.INT64_NULL, "mDEntryPrevSize": c.INT64_NULL}


def field(row, name, default=0):
    fields = row["fields"]
    if name in fields:
        return fields[name]
    if name in row.get("absent", ()):
        return ABSENT_AS.get(name, default)
    return default


def match_event_indicator(row):
    """The message's matchEventIndicator, or None if it has none (templates
    1, 2, 12, 55, unknown templates, or a version that lacks it)."""
    if row["templateId"] not in HAS_MEI:
        return None
    return row["fields"].get("matchEventIndicator")


def flags_of(mei):
    if mei is None:
        return 0
    return ((c.FLAG_END_OF_EVENT if mei & END_OF_EVENT_BIT else 0)
            | (c.FLAG_RECOVERY if mei & RECOVERY_BIT else 0)
            | (c.FLAG_IMPLIED if mei & IMPLIED_BIT else 0))


# ---- 2. One message -> its port 1 record (or None if not on port 1) ----
def to_event(row):
    t = row["templateId"]
    base = dict(seq_num=row["sequenceNumber"], seq_version=row["sequenceVersion"],
                channel=row["channel"], msg_index=row["index"], venue_msg_type=t,
                flags=flags_of(match_event_indicator(row)))
    f = lambda name, default=0: field(row, name, default)  # noqa: E731
    exch = f(TIME_FIELD[t]) if t in TIME_FIELD else 0

    if t == 50:
        action = f("mDUpdateAction")
        if action not in (MD_NEW, MD_CHANGE):
            return None
        common = dict(base, side=SIDES.get(f("mDEntryType"), c.SIDE_NONE),
                      rpt_seq=f("rptSeq"), instrument_id=f("securityID"),
                      price=f("mDEntryPx"), qty=f("mDEntrySize"),
                      order_id=f("secondaryOrderID"), exch_time=exch)
        if action == MD_NEW:
            return c.Event(kind=c.ADD, aux=f("enteringFirm"), **common)
        return c.Event(kind=c.MODIFY, aux=f("mDEntryPrevSize", c.INT64_NULL), **common)
    if t == 51:
        return c.Event(kind=c.DELETE, side=SIDES.get(f("mDEntryType"), c.SIDE_NONE),
                       rpt_seq=f("rptSeq"), instrument_id=f("securityID"),
                       price=f("mDEntryPx", c.INT64_NULL), qty=f("mDEntrySize"),
                       order_id=f("secondaryOrderID"), exch_time=exch, **base)
    if t == 52:
        if f("mDUpdateAction") != MD_DELETE_THRU:
            return None
        return c.Event(kind=c.CLEAR_SIDE, side=SIDES.get(f("mDEntryType"), c.SIDE_NONE),
                       rpt_seq=f("rptSeq"), instrument_id=f("securityID"),
                       exch_time=exch, **base)
    if t == 9:
        return c.Event(kind=c.CLEAR_BOOK, instrument_id=f("securityID"), exch_time=exch, **base)
    if t == 11:
        return c.Event(kind=c.CLEAR_CHANNEL, exch_time=exch, **base)
    if t == 55:
        return c.Event(kind=c.EXEC_SUMMARY, rpt_seq=f("rptSeq"), instrument_id=f("securityID"),
                       price=f("lastPx"), qty=f("fillQty"), exch_time=exch,
                       aux=f("aggressorSide"), **base)
    if t == 53:
        return c.Event(kind=c.TRADE, rpt_seq=f("rptSeq"), instrument_id=f("securityID"),
                       price=f("mDEntryPx"), qty=f("mDEntrySize"), order_id=f("tradeID"),
                       exch_time=exch,
                       aux=(f("mDEntryBuyer") << 32) | f("mDEntrySeller"), **base)
    if t == 57:
        return c.Event(kind=c.TRADE_BUST, rpt_seq=f("rptSeq"), instrument_id=f("securityID"),
                       price=f("mDEntryPx"), qty=f("mDEntrySize"), order_id=f("tradeID"),
                       exch_time=exch, **base)
    if t == 10:
        group = f("securityGroup", "").encode("ascii").ljust(3, b"\x00")[:3]
        return c.Event(kind=c.SESSION, exch_time=exch,
                       aux=session_aux(f("tradingSessionID"), f("tradingSessionSubID"),
                                       f("securityTradingEvent", 255), f("tradeDate"), group),
                       **base)
    if t == 3:
        return c.Event(kind=c.SESSION, rpt_seq=f("rptSeq"), instrument_id=f("securityID"),
                       exch_time=exch,
                       aux=session_aux(f("tradingSessionID"), f("securityTradingStatus"),
                                       f("securityTradingEvent", 255), f("tradeDate"),
                                       b"\x00\x00\x00"),
                       **base)
    if t == 1:
        return c.Event(kind=c.SEQ_RESET, **base)
    return None


def session_aux(session_id, sub_id_or_status, trading_event, trade_date, group):
    """aux of a SESSION record: bytes 0..7 = sessionID, subID/status, event,
    tradeDate (2 bytes LE), securityGroup (3 ASCII bytes)."""
    return int.from_bytes(bytes([session_id, sub_id_or_status, trading_event])
                          + trade_date.to_bytes(2, "little") + group, "little")


# ---- 3. The event-closing state machine (DESIGN.md section 3.5) ----
class Closer:
    """Per channel: IDLE (no event open) or OPEN. Feeds every decoded message
    through, in decoder order, and yields the port 1 records."""

    def __init__(self):
        self.open = {}   # channel -> True while an event is open

    def process(self, row):
        event = to_event(row)
        mei = match_event_indicator(row)
        ch = row["channel"]
        if mei is None:
            # No matchEventIndicator. ExecutionSummary always starts an event.
            if row["templateId"] == 55:
                self.open[ch] = True
            if event:
                yield event
            return
        if mei & END_OF_EVENT_BIT:
            was_open = self.open.get(ch, False)
            self.open[ch] = False
            if event:
                yield event                      # carries flag bit 0
            elif was_open:
                yield end_of_event_marker(row, mei)
            return
        self.open[ch] = True
        if event:
            yield event


def end_of_event_marker(row, mei):
    t = row["templateId"]
    return c.Event(seq_num=row["sequenceNumber"], seq_version=row["sequenceVersion"],
                   channel=row["channel"], msg_index=row["index"], kind=c.END_OF_EVENT,
                   flags=flags_of(mei), venue_msg_type=t,
                   exch_time=field(row, TIME_FIELD[t]) if t in TIME_FIELD else 0)


def convert(rows):
    """Port 1 records for decoded rows, in order."""
    closer = Closer()
    for row in rows:
        yield from closer.process(row)


# ---- 4. Command line ----
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("intent", help="intent.jsonl")
    ap.add_argument("-o", "--out", required=True, help="events.bin to write")
    ap.add_argument("--manifest", help="manifest.json (default: next to intent.jsonl)")
    ap.add_argument("--feed", default="A", choices=("A", "B"))
    ap.add_argument("--published-depth", type=int, default=c.DEFAULT_PUBLISHED_DEPTH)
    ap.add_argument("--tracked-depth", type=int, default=c.DEFAULT_TRACKED_DEPTH,
                    help="K recorded in the file header (0 = uncapped)")
    ap.add_argument("--max-instruments", type=int, default=c.DEFAULT_MAX_INSTRUMENTS)
    args = ap.parse_args(argv)

    if args.feed == "B":
        print("error: Feed B's cross-channel packet order is set by its jittered pcap "
              "timestamps, which intent.jsonl does not record (DESIGN.md section 3.7)",
              file=sys.stderr)
        return 2
    manifest = b3intent.load_manifest(args.intent, args.manifest)
    rows = (row for _, row in b3intent.feed_rows(args.intent, manifest, args.feed))
    header = c.events_header(args.published_depth, args.tracked_depth, args.max_instruments)
    count = 0
    with open(args.out, "wb") as out:
        out.write(header.pack())
        for event in convert(rows):
            out.write(event.pack())
            count += 1
    print(f"{args.out}: {count} records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
