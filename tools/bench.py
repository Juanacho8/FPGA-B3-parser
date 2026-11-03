#!/usr/bin/env python3
"""bench: run b3_bench (the benchmark harness) per build and set, and collect the results.

What it does:
  For each build (NAME:PATH to a b3_bench) and each set (Feed A of
  DATA/<set>), gives b3_bench the goldens for that build's parameters
  (read from its own counters: N, K, book mode):
    port 1  DATA/golden/<set>/expected_feedA.bin (the converter; the same
            records in every build, b3_bench checks the header's N and K)
    port 2  DATA/golden/<set>/expected_book_n<N>_k<K>_feedA.bin for a capped
            build; for the uncapped build tools/expected_book.py's uncapped
            key, computed once and cached under the build folder
  b3_bench refuses to time a build whose outputs differ (exit 1), and a
  machine on battery or in low power mode unless --allow-battery. Writes
  OUT/<set>_<build>.json and .md per run, and OUT/summary.md with every
  table.

What it must not do:
  Time anything itself, or report a run that failed its golden check.

Usage:
  python3 tools/bench.py --build k16:cpp_baseline/build/timed-k16/b3_bench \\
      [--build ...] [--set large] [--set burst] [--passes 30] --out docs/bench/results DATA_DIR
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import expected_book     # noqa: E402
import gen_instruments   # noqa: E402


def build_params(binary, table, pcap, tmp):
    """(N, K, book mode) of a b3_bench's build, from its b3_decode beside it."""
    import json
    decode = Path(binary).with_name("b3_decode")
    out = subprocess.run([str(decode), "--instruments", str(table), "--counters", str(tmp / "c.json"), str(pcap)],
                         check=True, capture_output=True, text=True)
    del out
    p = json.loads((tmp / "c.json").read_text())["params"]
    return p["published_depth"], p["tracked_depth"] if p["book"] == "capped" else 0, p["book"]


def golden_book(data, set_name, n, k, cache):
    folder = data / set_name
    if k:
        path = data / "golden" / set_name / f"expected_book_n{n}_k{k}_feedA.bin"
        if not path.is_file():
            raise SystemExit(f"error: no golden {path}: run make goldens")
        return path
    path = cache / f"expected_book_n{n}_uncapped_{set_name.replace('/', '_')}_feedA.bin"
    if not path.is_file():
        print(f"computing the uncapped port 2 key for {set_name} (tools/expected_book.py)...", flush=True)
        port2, _ = expected_book.run(folder, "A", n=n)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected_book.book_bytes(port2))
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data")
    ap.add_argument("--build", action="append", required=True, help="NAME:PATH to b3_bench")
    ap.add_argument("--set", action="append", help="set folder under DATA (default: large, burst)")
    ap.add_argument("--passes", type=int, default=30)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default=str(REPO / "cpp_baseline" / "build" / "bench_cache"))
    ap.add_argument("--allow-battery", action="store_true")
    args = ap.parse_args(argv)
    data, out, cache = Path(args.data), Path(args.out), Path(args.cache)
    out.mkdir(parents=True, exist_ok=True)
    sets = args.set or ["large", "burst"]
    summary = []
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        table = tmp / "instruments.txt"
        table.write_text(gen_instruments.table_text(REPO / "feed_generator" / "config.toml"))
        for set_name in sets:
            pcap = data / set_name / "feedA.pcap"
            for spec in args.build:
                name, binary = spec.split(":", 1)
                n, k, book = build_params(binary, table, pcap, tmp)
                prefix = out / f"{set_name.replace('/', '_')}_{name}"
                cmd = [binary, "--instruments", str(table), "--golden-events",
                       str(data / "golden" / set_name / "expected_feedA.bin"),
                       "--golden-book", str(golden_book(data, set_name, n, k, cache)),
                       "--passes", str(args.passes), "--out", str(prefix), str(pcap)]
                if args.allow_battery:
                    cmd.append("--allow-battery")
                print(f"== {set_name} / {name} ({book}, N={n}, K={k})", flush=True)
                res = subprocess.run(cmd, capture_output=True, text=True)
                sys.stdout.write(res.stdout)
                if res.returncode != 0:
                    failed += 1
                    sys.stderr.write(res.stderr)
                    summary.append(f"**{set_name} / {name}**: FAILED (exit {res.returncode}): {res.stderr.strip()}\n")
                    continue
                summary.append(prefix.with_suffix(".md").read_text())
    (out / "summary.md").write_text("\n".join(summary))
    print(f"results: {out}/summary.md")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
