#!/usr/bin/env python3
"""book_compare: b3_decode --book (port 2, uncapped book) against the answer keys.

What it does:
  For every generator set under a data folder and both feeds:
    * book.bin from b3_decode (the B3_BOOK=uncapped build) must be
      byte-identical to tools/expected_book.py's. The expected file follows
      the feed's pcap order (read with the generator's independent reader),
      so Feed B is compared in full order too, output sequence numbers
      included.
    * the book key: at every book_key.jsonl checkpoint, each instrument that
      is CLEAN when its channel reaches as_of must show the key's top 5
      levels per side. Checked on the C++ (b3_decode --checkpoints) and on
      the expected book itself. A placement the feed never delivers (lost,
      or in a discarded tail) is counted; the instrument is STALE there.
    * counters: no consistency error while CLEAN, no level overflow, no
      record past the buffer, and the C++ counts equal the expected ones.
  Sets in b3intent.EXPECTED_FAILURES report XFAIL, or XPASS (a failure) if
  they match. Fails on any difference, or if nothing was checked.

What it must not do:
  Import b3gen, or use C++ output to compute what it expects.

Usage:
  python3 tools/book_compare.py --cpp cpp_baseline/build/uncapped/b3_decode DATA_DIR
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

import b3contract as c   # noqa: E402
import b3intent           # noqa: E402
import b3replay           # noqa: E402
import counters_compare   # noqa: E402
import expected_book      # noqa: E402
import gen_instruments    # noqa: E402

N = 5   # book_key.jsonl's depth; the uncapped build's N (checked from book.bin's header)


def describe(rec):
    out_seq, seq, ver, ch, idx, sid, exch, cap, rtype, state, flags, nb, no, cause = c.BOOK_HEADER.unpack_from(rec, 0)
    return (f"out_seq {out_seq} {c.RECORD_TYPE_NAMES.get(rtype, rtype)} channel {ch} v{ver} packet {seq} "
            f"message {idx} instr {sid} state {state} cause {cause} flags {flags:#x} levels {nb}/{no} capture {cap}")


def first_difference(want, got):
    size = struct.unpack_from("<I", want, 12)[0]
    if want[:32] != got[:32]:
        return "file header differs"
    for i, off in enumerate(range(32, min(len(want), len(got)), size)):
        if want[off:off + size] != got[off:off + size]:
            return (f"record {i}: want {describe(want[off:off + size])}\n"
                    f"        got  {describe(got[off:off + size])}")
    return f"{(len(want) - 32) // size} records expected, {(len(got) - 32) // size} written"


def checkpoint_plan(book_key, ids):
    """[(placement, securityID, bids, offers)] from book_key.jsonl."""
    plan = []
    for line in open(book_key, encoding="utf-8"):
        row = json.loads(line)
        for name, b in row["books"].items():
            if name not in ids:
                raise b3replay.Broken(f"book key names {name}, which the intent log never mentions")
            plan.append((tuple(b["as_of"]), ids[name], [tuple(x) for x in b["bids"]],
                         [tuple(x) for x in b["offers"]]))
    return plan


def parse_dump(text):
    """{placement: {securityID: (state, bids, offers)}} from b3_decode --checkpoint-out."""
    out = {}
    for line in text.splitlines():
        t = line.split()
        placement, sid, state = tuple(int(x) for x in t[:4]), int(t[4]), t[5]
        pos = 6
        sides = []
        for _ in range(2):
            n = int(t[pos])
            pos += 1
            sides.append([tuple(int(x) for x in t[pos + 3 * i:pos + 3 * i + 3]) for i in range(n)])
            pos += 3 * n
        out.setdefault(placement, {})[sid] = (state, sides[0], sides[1])
    return out


def check_key(plan, books):
    """(checked, unreached, [problem]): CLEAN instruments' top 5 against the key."""
    checked = unreached = 0
    problems = []
    for placement, sid, bids, offers in plan:
        snap = books.get(placement)
        if snap is None:
            unreached += 1
            continue
        state, got_bids, got_offers = snap[sid]
        if state != "CLEAN":
            continue
        checked += 1
        if (got_bids, got_offers) != (bids, offers):
            problems.append(f"book key at {list(placement)} instrument {sid}: want bids {bids} offers "
                            f"{offers}, got bids {got_bids} offers {got_offers}")
    return checked, unreached, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data", help="generator output folder (make data)")
    ap.add_argument("--cpp", required=True, help="b3_decode of the B3_BOOK=uncapped build")
    args = ap.parse_args(argv)

    root = Path(args.data)
    sets = sorted(p.parent for p in root.rglob("intent.jsonl") if "golden" not in p.parts)
    if not sets:
        print(f"error: no generator sets under {root}: run make data", file=sys.stderr)
        return 2
    checked = failed = xfail = 0
    key_total = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        table_path = tmp / "instruments.txt"
        table_path.write_text(gen_instruments.table_text(REPO / "feed_generator" / "config.toml"))
        table = b3replay.read_table(table_path)
        for folder in sets:
            name = folder.relative_to(root)
            manifest = b3intent.load_manifest(folder / "intent.jsonl")
            reason = b3intent.expected_failure(manifest)
            ids = counters_compare.names_to_ids(folder / "intent.jsonl")
            plan = checkpoint_plan(folder / "book_key.jsonl", ids)
            placements = {p for p, *_ in plan}
            (tmp / "checkpoints.txt").write_text("".join(" ".join(map(str, p)) + "\n" for p in sorted(placements)))
            for feed in ("A", "B"):
                pcap = folder / f"feed{feed}.pcap"
                if not pcap.is_file():
                    print(f"SKIPPED {name}/{pcap.name}: no such file in this set")
                    continue
                checked += 1
                problems = []
                res = subprocess.run([str(args.cpp), "--instruments", str(table_path), "--book", str(tmp / "cpp.bin"),
                                      "--checkpoints", str(tmp / "checkpoints.txt"),
                                      "--checkpoint-out", str(tmp / "dump.txt"), str(pcap)],
                                     check=True, capture_output=True, text=True)
                got_counts = json.loads(res.stdout)["book"]
                got = (tmp / "cpp.bin").read_bytes()
                if struct.unpack_from("<H", got, 16)[0] != N:
                    problems.append(f"book.bin N is {struct.unpack_from('<H', got, 16)[0]}, the key's depth is {N}")
                try:
                    port2, snapshots = expected_book.run(folder, feed, table, checkpoints=placements)
                except b3replay.Broken as e:
                    problems.append(f"answer key: {e}")
                    port2 = None
                if port2 is not None:
                    want = expected_book.book_bytes(port2)
                    if want != got:
                        problems.append(first_difference(want, got))
                    b = port2.book
                    want_counts = dict(port2.counts, level_overflow=b.level_overflow, consistency_errors=b.errors,
                                       consistency_errors_stale=b.errors_stale, null_price_orders=b.null_price,
                                       overflow=0, records=len(port2.records))
                    for k, v in want_counts.items():
                        if got_counts.get(k) != v:
                            problems.append(f"{k}: keys {v}, c++ {got_counts.get(k)}")
                    _, _, key_py = check_key(plan, snapshots)
                    problems += [f"answer key itself: {p}" for p in key_py[:3]]
                for k in ("consistency_errors", "level_overflow", "overflow"):
                    if got_counts.get(k):
                        problems.append(f"{k} = {got_counts[k]} (must be 0)")
                n_key, unreached, key_cpp = check_key(plan, parse_dump((tmp / "dump.txt").read_text()))
                problems += key_cpp
                key_total += n_key
                label = f"{name}/{pcap.name}"
                if reason:
                    if problems:
                        xfail += 1
                        print(f"XFAIL {label}: {reason} ({len(problems)} differences, first: {problems[0][:120]})")
                    else:
                        failed += 1
                        print(f"XPASS {label}: matches although expected to fail ({reason}): remove it from "
                              f"b3intent.EXPECTED_FAILURES")
                elif problems:
                    failed += 1
                    print(f"MISMATCH {label}")
                    for p in problems[:8]:
                        print(f"    {p}")
                else:
                    print(f"OK {label}: {len(port2.records)} records byte-identical; book key {n_key} CLEAN "
                          f"checkpoints match" + (f", {unreached} not delivered" if unreached else "") +
                          f"; stale-time consistency errors {got_counts['consistency_errors_stale']}")
    print(f"{checked - failed - xfail}/{checked} files match in {len(sets)} sets, {xfail} expected failures; "
          f"{key_total} CLEAN book-key checkpoints checked")
    if checked == 0:
        print("error: nothing was checked", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
