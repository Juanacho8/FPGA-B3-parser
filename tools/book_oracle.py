#!/usr/bin/env python3
"""book_oracle: the capped book against the uncapped book, as an oracle.

What it does:
  Runs a capped b3_decode build and an uncapped build with the same N on
  every generator set under a data folder and both feeds, and compares the
  two book.bin files record by record. They must be identical except for:
    * the K field of the file header (offset 18): K in the capped file, 0 in
      the uncapped one
    * the DEPTH_INCOMPLETE_BID/OFFER flags (byte 42, bits 1 and 2), which
      the uncapped book never sets
    * on a side the capped record flags depth incomplete, its level count
      (byte 43 or 44) and its levels
  Every other difference is a bug: where a side is not depth incomplete, the
  capped book's published levels are the true top N (brief section 5).
  Then reports, per (N, K), from the capped build's counters: how often each
  side went depth incomplete and for how long, how many times it truncated,
  levels dropped and updates ignored, the most valid slots, and the most
  levels the uncapped book saw per side. Fails on any difference, or if
  nothing was compared.

What it must not do:
  Use an answer key: this is C++ against C++, a second check beside
  tools/book_compare.py (each build against tools/expected_book.py).

Usage:
  python3 tools/book_oracle.py --capped cpp_baseline/build/k16/b3_decode \\
      --uncapped cpp_baseline/build/uncapped/b3_decode DATA_DIR
"""

import argparse
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import b3contract as c       # noqa: E402
import gen_instruments       # noqa: E402

FLAGS, BID_COUNT, OFFER_COUNT = 42, 43, 44
DEPTH_FLAGS = c.BOOK_FLAG_DEPTH_INCOMPLETE_BID | c.BOOK_FLAG_DEPTH_INCOMPLETE_OFFER
SIDE_KEYS = ("truncations", "levels_dropped", "ignored", "depth_incomplete", "depth_incomplete_ns",
             "depth_incomplete_max_ns", "max_valid")


def masked(rec, flags, n):
    """The record with what may differ zeroed: the depth flags, and the
    count and levels of each side that flags (the capped record's) mark
    depth incomplete."""
    b = bytearray(rec)
    b[FLAGS] &= ~DEPTH_FLAGS & 0xFF
    for bit, count, first in ((c.BOOK_FLAG_DEPTH_INCOMPLETE_BID, BID_COUNT, 0),
                              (c.BOOK_FLAG_DEPTH_INCOMPLETE_OFFER, OFFER_COUNT, n)):
        if flags & bit:
            b[count] = 0
            start = c.BOOK_HEADER.size + c.LEVEL.size * first
            b[start:start + c.LEVEL.size * n] = bytes(c.LEVEL.size * n)
    return bytes(b)


def compare(capped, uncapped):
    """([problem], records, records flagged depth incomplete)."""
    problems = []
    hc, hu = c.FileHeader.unpack(capped), c.FileHeader.unpack(uncapped)
    if hu.tracked_depth != 0:
        problems.append(f"uncapped file header K = {hu.tracked_depth}, want 0")
    if hc.tracked_depth == 0:
        problems.append("capped file header K = 0")
    if capped[:18] + capped[20:32] != uncapped[:18] + uncapped[20:32]:
        problems.append("file headers differ beyond K")
    n, size = hc.published_depth, hc.record_size
    body_c, body_u = capped[32:], uncapped[32:]
    if len(body_c) != len(body_u):
        problems.append(f"{len(body_c) // size} capped records, {len(body_u) // size} uncapped")
    flagged = 0
    for i, off in enumerate(range(0, min(len(body_c), len(body_u)), size)):
        rc, ru = body_c[off:off + size], body_u[off:off + size]
        if ru[FLAGS] & DEPTH_FLAGS:
            problems.append(f"record {i}: the uncapped book set a depth-incomplete flag")
        if rc[FLAGS] & DEPTH_FLAGS:
            flagged += 1
        if masked(rc, rc[FLAGS], n) != masked(ru, rc[FLAGS], n):
            r = c.BookRecord.unpack_from(rc, 0, n)
            u = c.BookRecord.unpack_from(ru, 0, n)
            problems.append(f"record {i} (out_seq {r.out_seq}, {c.RECORD_TYPE_NAMES.get(r.record_type)}, "
                            f"instrument {r.instrument_id}, flags {r.flags:#x}): capped bids {r.bids} offers "
                            f"{r.offers}, uncapped bids {u.bids} offers {u.offers}")
        if len(problems) > 8:
            break
    return problems, len(body_c) // size, flagged


def run(binary, table, pcap, out):
    res = subprocess.run([str(binary), "--instruments", str(table), "--book", str(out), str(pcap)],
                         check=True, capture_output=True, text=True)
    return json.loads(res.stdout)["book"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data", help="generator output folder (make data)")
    ap.add_argument("--capped", required=True, help="b3_decode of a capped build")
    ap.add_argument("--uncapped", required=True, help="b3_decode of an uncapped build with the same N")
    args = ap.parse_args(argv)

    root = Path(args.data)
    sets = sorted(p.parent for p in root.rglob("intent.jsonl") if "golden" not in p.parts)
    if not sets:
        print(f"error: no generator sets under {root}: run make data", file=sys.stderr)
        return 2
    checked = failed = 0
    total = {side: dict.fromkeys(SIDE_KEYS, 0) for side in ("bid", "offer")}
    max_depth = {"bid": 0, "offer": 0}
    records = flagged_total = 0
    params = None
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        table = tmp / "instruments.txt"
        table.write_text(gen_instruments.table_text(REPO / "feed_generator" / "config.toml"))
        for folder in sets:
            name = folder.relative_to(root)
            for feed in ("A", "B"):
                pcap = folder / f"feed{feed}.pcap"
                if not pcap.is_file():
                    print(f"SKIPPED {name}/{pcap.name}: no such file in this set")
                    continue
                checked += 1
                cb = run(args.capped, table, pcap, tmp / "capped.bin")
                ub = run(args.uncapped, table, pcap, tmp / "uncapped.bin")
                if cb["book_mode"] != "capped" or ub["book_mode"] != "uncapped":
                    print(f"error: --capped is a {cb['book_mode']} build, --uncapped a {ub['book_mode']} build",
                          file=sys.stderr)
                    return 2
                if cb["published_depth"] != ub["published_depth"]:
                    print(f"error: N differs: capped {cb['published_depth']}, uncapped {ub['published_depth']}",
                          file=sys.stderr)
                    return 2
                params = (cb["published_depth"], cb["tracked_depth"])
                problems, n_rec, flagged = compare((tmp / "capped.bin").read_bytes(),
                                                   (tmp / "uncapped.bin").read_bytes())
                records += n_rec
                flagged_total += flagged
                for side in ("bid", "offer"):
                    st = cb["sides"][side]
                    for k in SIDE_KEYS:
                        if k in ("depth_incomplete_max_ns", "max_valid"):
                            total[side][k] = max(total[side][k], st[k])
                        else:
                            total[side][k] += st[k]
                    max_depth[side] = max(max_depth[side], ub["max_depth"][side])
                label = f"{name}/{pcap.name}"
                trunc = cb["sides"]["bid"]["truncations"] + cb["sides"]["offer"]["truncations"]
                note = (f"; truncations {trunc}, depth-incomplete episodes "
                        f"{cb['sides']['bid']['depth_incomplete']}/{cb['sides']['offer']['depth_incomplete']} "
                        f"(bid/offer), {flagged} records flagged" if trunc else "")
                if problems:
                    failed += 1
                    print(f"MISMATCH {label}")
                    for p in problems[:8]:
                        print(f"    {p}")
                else:
                    print(f"OK {label}: {n_rec} records{note}")
    if checked == 0:
        print("error: nothing was compared", file=sys.stderr)
        return 1
    n, k = params
    print(f"N={n}, K={k}: {checked - failed}/{checked} files match the uncapped book in {len(sets)} sets; "
          f"{records} records, {flagged_total} flagged depth incomplete")
    for side in ("bid", "offer"):
        t = total[side]
        print(f"  {side:5}: truncations {t['truncations']}, levels dropped {t['levels_dropped']}, "
              f"ignored {t['ignored']}, depth-incomplete episodes {t['depth_incomplete']} "
              f"lasting {t['depth_incomplete_ns'] / 1e9:.6f} s in all (longest "
              f"{t['depth_incomplete_max_ns'] / 1e9:.6f} s), most valid slots {t['max_valid']}, "
              f"most levels (uncapped) {max_depth[side]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
