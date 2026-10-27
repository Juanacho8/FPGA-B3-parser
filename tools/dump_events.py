#!/usr/bin/env python3
"""dump_events: prints an events.bin as readable rows, for debugging.

What it does:
  Reads the file header (layout from the header, never assumed), then one
  line per 64-byte port 1 record. --hex adds each record's raw bytes.

What it must not do:
  Interpret records beyond docs/DESIGN.md section 3; it is a viewer.

Usage:
  python3 tools/dump_events.py events.bin [--hex] [--limit 50] [--skip 1000]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import b3contract as c   # noqa: E402

SIDE = {0: "-", 1: "BID", 2: "OFFER"}


def fmt_flags(flags):
    names = [n for bit, n in ((c.FLAG_END_OF_EVENT, "EOE"), (c.FLAG_RECOVERY, "REC"),
                              (c.FLAG_IMPLIED, "IMP"), (c.FLAG_BOOK_NOT_CLEAN, "NCLEAN"))
             if flags & bit]
    if flags & ~0x0F:
        names.append(f"0x{flags & ~0x0F:02x}?")
    return "|".join(names) or "-"


def fmt_price(price):
    return "null" if price == c.INT64_NULL else str(price)


def fmt_event(e):
    return (f"{e.channel:3d} v{e.seq_version} #{e.seq_num}.{e.msg_index:<3d} "
            f"{c.KIND_NAMES.get(e.kind, f'kind{e.kind}'):<13} t{e.venue_msg_type:<3d} "
            f"{fmt_flags(e.flags):<11} {SIDE.get(e.side, e.side):<5} "
            f"instr={e.instrument_id} rpt={e.rpt_seq} px={fmt_price(e.price)} qty={e.qty} "
            f"id={e.order_id} t={e.exch_time} aux={e.aux:#x}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("events", help="events.bin")
    ap.add_argument("--hex", action="store_true", help="also print each record's bytes")
    ap.add_argument("--skip", type=int, default=0, help="records to skip")
    ap.add_argument("--limit", type=int, default=None, help="records to print")
    args = ap.parse_args(argv)

    data = Path(args.events).read_bytes()
    header, events = c.read_events(data)
    k = "uncapped" if header.tracked_depth == 0 else header.tracked_depth
    print(f"# {args.events}: format v{header.format_version}, {len(events)} records of "
          f"{header.record_size} B, N={header.published_depth} K={k} "
          f"instruments={header.max_instruments}")
    end = len(events) if args.limit is None else args.skip + args.limit
    for i in range(args.skip, min(end, len(events))):
        print(f"{i:8d} {fmt_event(events[i])}")
        if args.hex:
            off = header.header_size + i * header.record_size
            raw = data[off:off + header.record_size]
            for j in range(0, len(raw), 16):
                print(f"{'':9}+{j:02d}  {raw[j:j + 16].hex(' ')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
