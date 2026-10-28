#!/usr/bin/env python3
"""mbo_check: the C++ MBO book (b3_decode --mbo) on every set and both feeds.

What it does:
  Runs the uncapped b3_decode with --mbo on both feeds of every generator
  set under a data folder (the MBO book is built from port 1 records only,
  DESIGN.md 4.8) and checks:
    * the per-order consistency checks: 0 faults of every reason while the
      instrument is CLEAN (faults while STALE are reported, expected)
    * the three-way check: after every event close, each touched CLEAN
      instrument's per-price totals in the MBO book equal the uncapped
      book's on every level of both sides (0 level mismatches)
    * the book key: at every book_key.jsonl checkpoint, each instrument
      CLEAN when its channel reaches as_of shows the key's top 5 levels in
      the MBO book (a per-order rebuild against the generator's own book)
    * no pool or table overflow
  Reports the MBO book's state size, peak resting orders per instrument and
  in total. Sets in b3intent.EXPECTED_FAILURES report XFAIL whatever their
  result (shown): these checks compare the C++ with itself and with the key
  only at CLEAN points, so a pass is no sign the missing feature works, and
  is no XPASS. Fails on any difference, or if nothing was checked.

What it must not do:
  Import b3gen, or use C++ output to compute what it expects.

Usage:
  python3 tools/mbo_check.py --cpp cpp_baseline/build/uncapped/b3_decode DATA_DIR
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import b3intent           # noqa: E402
import book_compare       # noqa: E402
import counters_compare   # noqa: E402
import gen_instruments    # noqa: E402


def check_run(mbo, plan, dump):
    """([problem], key checkpoints checked, unreached) for one feed's run."""
    problems = []
    for reason, n in mbo["faults"].items():
        if n:
            problems.append(f"{reason}: {n} while CLEAN (must be 0)")
    if mbo["level_mismatches"]:
        problems.append(f"{mbo['level_mismatches']} level mismatches against the uncapped book, first: "
                        + "; ".join(json.dumps(m) for m in mbo["mismatches"][:3]))
    for k in ("pool_overflow", "table_overflow"):
        if mbo[k]:
            problems.append(f"{k} = {mbo[k]} (must be 0)")
    checked, unreached, _, key = book_compare.check_key(plan, book_compare.parse_dump(dump))
    problems += key
    return problems, checked, unreached


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data", help="generator output folder (make data)")
    ap.add_argument("--cpp", required=True, help="b3_decode of the B3_BOOK=uncapped build")
    ap.add_argument("--mbo-orders", type=int, default=262144, help="the MBO book's order capacity")
    args = ap.parse_args(argv)

    root = Path(args.data)
    sets = sorted(p.parent for p in root.rglob("intent.jsonl") if "golden" not in p.parts)
    if not sets:
        print(f"error: no generator sets under {root}: run make data", file=sys.stderr)
        return 2
    checked = failed = xfail = 0
    key_total = 0
    peak_total = peak_instrument = 0
    stale_faults = 0
    state_bytes = None
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        table = tmp / "instruments.txt"
        table.write_text(gen_instruments.table_text(REPO / "feed_generator" / "config.toml"))
        for folder in sets:
            name = folder.relative_to(root)
            manifest = b3intent.load_manifest(folder / "intent.jsonl")
            reason = b3intent.expected_failure(manifest)
            ids = counters_compare.names_to_ids(folder / "intent.jsonl")
            plan = book_compare.checkpoint_plan(folder / "book_key.jsonl", ids)
            placements = sorted({p for p, *_ in plan})
            (tmp / "checkpoints.txt").write_text("".join(" ".join(map(str, p)) + "\n" for p in placements))
            for feed in ("A", "B"):
                pcap = folder / f"feed{feed}.pcap"
                if not pcap.is_file():
                    print(f"SKIPPED {name}/{pcap.name}: no such file in this set")
                    continue
                checked += 1
                res = subprocess.run([str(args.cpp), "--instruments", str(table), "--mbo",
                                      "--mbo-orders", str(args.mbo_orders),
                                      "--checkpoints", str(tmp / "checkpoints.txt"),
                                      "--checkpoint-out", str(tmp / "dump.txt"),
                                      "--mbo-checkpoint-out", str(tmp / "mbo.txt"), str(pcap)],
                                     capture_output=True, text=True)
                if res.returncode != 0:
                    print(f"error: {args.cpp} failed: {res.stderr.strip()}", file=sys.stderr)
                    return 2
                out = json.loads(res.stdout)
                if out["book"]["published_depth"] != book_compare.KEY_DEPTH:
                    print(f"error: N = {out['book']['published_depth']}; the book key holds "
                          f"{book_compare.KEY_DEPTH} levels", file=sys.stderr)
                    return 2
                mbo = out["mbo"]
                state_bytes = mbo["state_bytes"]
                problems, n_key, unreached = check_run(mbo, plan, (tmp / "mbo.txt").read_text())
                key_total += n_key
                peak_total = max(peak_total, mbo["peak_orders"])
                peak_instrument = max(peak_instrument, mbo["peak_orders_per_instrument"])
                stale = sum(mbo["faults_stale"].values())
                stale_faults += stale
                label = f"{name}/{pcap.name}"
                if reason:
                    # Always XFAIL: these checks compare the C++ with itself and with the key at
                    # CLEAN points only, so passing them says nothing about the missing feature.
                    xfail += 1
                    print(f"XFAIL {label}: {reason} (" + (f"{len(problems)} differences, first: {problems[0][:120]}"
                                                          if problems else "its checks pass here") + ")")
                elif problems:
                    failed += 1
                    print(f"MISMATCH {label}")
                    for p in problems[:8]:
                        print(f"    {p}")
                else:
                    stale_note = ", ".join(f"{k} {v}" for k, v in mbo["faults_stale"].items() if v)
                    print(f"OK {label}: {mbo['instrument_checks']} CLEAN instrument checks at "
                          f"{mbo['closes']} closes, 0 level mismatches; book key {n_key} CLEAN checkpoints match"
                          + (f", {unreached} not delivered" if unreached else "")
                          + f"; peak orders {mbo['peak_orders']} ({mbo['peak_orders_per_instrument']} on one "
                          f"instrument)" + (f"; while STALE: {stale_note}" if stale_note else ""))
    print(f"{checked - failed - xfail}/{checked} files pass in {len(sets)} sets, {xfail} expected failures; "
          f"{key_total} CLEAN book-key checkpoints checked; MBO state {state_bytes} bytes, peak resting orders "
          f"{peak_total} in total and {peak_instrument} on one instrument; {stale_faults} faults while STALE")
    if checked == 0:
        print("error: nothing was checked", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
