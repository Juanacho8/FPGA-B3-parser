#!/usr/bin/env python3
"""profile: where b3_bench's time goes, by pipeline stage (macOS /usr/bin/sample).

What it does:
  Runs b3_bench of a profile build (B3_PROFILE=ON: each stage's entry point
  out of line, cpp_baseline/include/b3/stage.hpp) on one pcap with many
  throughput passes (--no-latency: no clock reads in the pipeline, no
  sorting), attaches the macOS sampler (/usr/bin/sample, 1 ms) once the
  golden check is over, and reads its "top of stack" counts: each sample's
  innermost function, its self time. Groups them by stage:
    pcap index, packet decoder and sequencer, walker, field decode (the
    event sink's field reads), event sink, tracker, field guards (the
    tracker's per-message presence checks, open item N), book sink, book,
    harness (b3_bench's own timestamps and loop), other (the C++ library,
    the kernel)
  and prints the top functions and the per-stage totals as markdown.
  (Instruments' Time Profiler, xcrun xctrace, needs a full Xcode install;
  the Command Line Tools ship /usr/bin/sample.)

What it must not do:
  Be read as timing: a profile build adds a call per stage. Its shares are
  the point; b3_bench's timed builds give the times.

Usage:
  python3 tools/profile.py --bench cpp_baseline/build/profile-k16/b3_bench --golden-events E \\
      --golden-book B [--seconds 20] [--out docs/bench/profile.md] FILE.pcap
"""

import argparse
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import gen_instruments   # noqa: E402

# (stage, pattern on the demangled symbol), first match wins.
STAGES = [
    ("field guards", r"check_appliable"),
    ("field decode", r"event_sink::fill"),
    ("event sink", r"event_sink::EventSink"),
    ("tracker", r"tracker::Tracker"),
    ("book sink", r"book::BookSink"),
    ("book", r"book::(CappedBook|UncappedBook|NullBucket)"),
    ("walker", r"walker::|framing::"),
    ("packet decoder and sequencer", r"packet::"),
    ("pcap index", r"pcap::"),
    ("harness", r"Pipeline|main|ticks|mach_absolute_time|percentiles"),
]
LINE = re.compile(r"^\s+(.+?)\s+\(in ([^)]+)\)\s+(?:\+\s*\d+\s+)?.*?(\d+)\s*$")


def stage_of(symbol):
    for name, pat in STAGES:
        if re.search(pat, symbol):
            return name
    return "other"


def top_of_stack(text):
    """[(symbol, library, samples)] from sample's 'Sort by top of stack' section."""
    out = []
    if "Sort by top of stack" not in text:
        return out
    section = text.split("Sort by top of stack", 1)[1]
    for line in section.splitlines()[1:]:
        if not line.strip():
            if out:
                break
            continue
        m = re.match(r"^\s+(.*?)\s+\(in ([^)]+)\)\s+(\d+)\s*$", line)
        if m:
            out.append((m.group(1), m.group(2), int(m.group(3))))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pcap")
    ap.add_argument("--bench", required=True)
    ap.add_argument("--golden-events", required=True)
    ap.add_argument("--golden-book", required=True)
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--passes", type=int, default=400)
    ap.add_argument("--delay", type=float, default=4.0, help="seconds before sampling: load and golden check")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        table = tmp / "instruments.txt"
        table.write_text(gen_instruments.table_text(REPO / "feed_generator" / "config.toml"))
        bench = subprocess.Popen([args.bench, "--instruments", str(table), "--golden-events", args.golden_events,
                                  "--golden-book", args.golden_book, "--passes", str(args.passes), "--no-latency", args.pcap],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        time.sleep(args.delay)
        if bench.poll() is not None:
            print(f"error: b3_bench ended before sampling (exit {bench.returncode}): {bench.stderr.read()}",
                  file=sys.stderr)
            return 1
        report = tmp / "sample.txt"
        subprocess.run(["/usr/bin/sample", str(bench.pid), str(args.seconds), "1", "-mayDie", "-file", str(report)],
                       check=True, capture_output=True)
        bench.kill()
        bench.wait()
        rows = top_of_stack(report.read_text(errors="replace"))
    if not rows:
        print("error: no samples parsed", file=sys.stderr)
        return 1
    total = sum(n for _, _, n in rows)
    by_stage = {}
    for sym, _, n in rows:
        by_stage[stage_of(sym)] = by_stage.get(stage_of(sym), 0) + n
    lines = [f"Profile of `{Path(args.bench).parent.name}` on `{args.pcap}`: {total} samples (1 ms, top of stack, "
             f"self time), {args.seconds} s of timed passes.\n",
             "| Stage | Samples | Share |", "|---|---|---|"]
    for name, n in sorted(by_stage.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {name} | {n} | {100 * n / total:.1f}% |")
    lines += ["", "| Function (top of stack) | Stage | Samples | Share |", "|---|---|---|---|"]
    for sym, lib, n in sorted(rows, key=lambda r: -r[2])[:25]:
        short = re.sub(r"\(.*", "", sym)
        short = short if len(short) < 110 else short[:107] + "..."
        lines.append(f"| `{short}` ({lib}) | {stage_of(sym)} | {n} | {100 * n / total:.1f}% |")
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
