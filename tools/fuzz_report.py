#!/usr/bin/env python3
"""fuzz_report: run b3_fuzz (Debug, sanitizers) and report every outcome.

What it does:
  Runs cpp_baseline's b3_fuzz on a capture with the instrument table, then
  prints, per mutation kind: iterations, how many took, losses and
  unappliable messages they caused, and the iterations with problems while
  CLEAN (consistency errors in either price-level book, MBO faults, level
  mismatches), with the first few. Fails if b3_fuzz dies (a crash, or
  undefined behaviour: the sanitizers abort and it prints the iteration).
  Problems while CLEAN are reported, not failed on: a damaged field that
  still looks valid (a size, a price, a market order) cannot be detected
  from one feed, and the report says which mutation produced each.

Usage:
  python3 tools/fuzz_report.py --fuzz cpp_baseline/build/debug/b3_fuzz [--iterations N] [--seed S] FILE.pcap
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import gen_instruments   # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pcap")
    ap.add_argument("--fuzz", required=True)
    ap.add_argument("--iterations", type=int, default=1600)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        table = Path(tmp) / "instruments.txt"
        table.write_text(gen_instruments.table_text(REPO / "feed_generator" / "config.toml"))
        res = subprocess.run([args.fuzz, "--instruments", str(table), "--iterations", str(args.iterations),
                              "--seed", str(args.seed), args.pcap], capture_output=True, text=True)
    if res.returncode != 0:
        print(f"FAIL: b3_fuzz died (exit {res.returncode}): a crash or undefined behaviour")
        print(res.stderr[-4000:])
        return 1
    out = json.loads(res.stdout)
    print(f"{out['iterations']} iterations on {out['file']} (seed {out['seed']}): no crash, no undefined behaviour")
    for kind, t in out["kinds"].items():
        print(f"  {kind:13} {t['applied']:4}/{t['iterations']:<4} applied; losses {t['losses']}, unappliable "
              f"{t['unappliable']}; problems while CLEAN in {t['clean_error_iterations']} iterations "
              f"(uncapped {t['uncapped_errors']}, capped {t['capped_errors']}, MBO faults {t['mbo_faults']}, "
              f"level mismatches {t['mbo_mismatches']})")
        for f in t["first"]:
            print(f"      e.g. {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
