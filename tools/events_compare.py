#!/usr/bin/env python3
"""events_compare: b3_decode --events (port 1) against intent_to_events.py.

What it does:
  For every generator set under a data folder, both feeds, and every build
  given with --build (release, k16, k32, uncapped):
    * Feed A: the C++ events.bin must be byte-identical to
      tools/intent_to_events.py's, run with the build's N, K (0 = uncapped)
      and MAX_INSTRUMENTS, so the file headers agree too.
    * Feed B: intent.jsonl does not give Feed B's cross-channel order
      (docs/DESIGN.md 3.7), so the C++ records are compared record by record
      within each channel with intent_to_events.convert() run on Feed B's
      view of the intent log (tools/b3intent.py). The header must match too.
    * Across builds: the records (everything after the header) must be the
      same in every build; only the header's N and K may differ.
  The first difference is reported with its packet and message position, in
  dump_events.py's format. The C++'s counts of SequenceReset/ChannelReset
  arriving with an event open are reported (DESIGN.md 3.5 does not close an
  event on them).

What it must not do:
  Change or reimplement intent_to_events.py: it is the answer key. Import b3gen.

Usage:
  python3 tools/events_compare.py DATA_DIR --build release:cpp_baseline/build/release/b3_decode \\
      --build k32:cpp_baseline/build/k32/b3_decode ...
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import b3contract as c      # noqa: E402
import b3intent             # noqa: E402
import dump_events          # noqa: E402
import intent_to_events     # noqa: E402

HEADER = c.FILE_HEADER.size


def run_cpp(binary, pcap, out):
    res = subprocess.run([str(binary), "--events", str(out), str(pcap)], check=True,
                         capture_output=True, text=True)
    return json.loads(res.stdout)["events"]


def first_difference(want, got, label):
    """[problem] for the first differing record of two Event lists."""
    for i, (w, g) in enumerate(zip(want, got)):
        if w != g:
            return [f"{label} record {i}: first difference at channel {w.channel} v{w.seq_version} "
                    f"packet {w.seq_num} message {w.msg_index}",
                    f"    want {dump_events.fmt_event(w)}", f"    got  {dump_events.fmt_event(g)}"]
    if len(want) != len(got):
        extra = want[len(got)] if len(want) > len(got) else got[len(want)]
        return [f"{label}: {len(want)} records expected, {len(got)} written; first unmatched "
                f"{dump_events.fmt_event(extra)}"]
    return []


def per_channel(events):
    out = {}
    for e in events:
        out.setdefault(e.channel, []).append(e)
    return out


def expected_feed_b(intent, manifest, header):
    rows = (row for _, row in b3intent.feed_rows(intent, manifest, "B"))
    return header, list(intent_to_events.convert(rows))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data", help="generator output folder (make data)")
    ap.add_argument("--build", action="append", required=True,
                    help="NAME:PATH_TO_b3_decode (the build's N, K and MAX_INSTRUMENTS are read "
                         "from the file header it writes, and given to the converter)")
    args = ap.parse_args(argv)
    builds = [b.split(":", 1) for b in args.build]

    root = Path(args.data)
    sets = sorted(p.parent for p in root.rglob("intent.jsonl"))
    if not sets:
        print(f"error: no generator sets under {root}: run make data", file=sys.stderr)
        return 2
    checked = failed = 0
    reset_notes = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for folder in sets:
            name = folder.relative_to(root)
            intent = folder / "intent.jsonl"
            manifest = b3intent.load_manifest(intent)
            for feed in ("A", "B"):
                pcap = folder / f"feed{feed}.pcap"
                if not pcap.is_file():
                    print(f"SKIPPED {name}/{pcap.name}: no such file in this set")
                    continue
                records_by_build = {}
                problems = []
                for build, binary in builds:
                    checked += 1
                    got_path = tmp / f"cpp_{build}.bin"
                    counts = run_cpp(binary, pcap, got_path)
                    if counts["overflow"]:
                        problems.append(f"{build}: {counts['overflow']} records past the buffer's capacity")
                    if counts["resets_with_event_open"]:
                        reset_notes.append(f"{name}/{pcap.name} {build}: {counts['resets_with_event_open']}")
                    data = got_path.read_bytes()
                    header, got = c.read_events(data)
                    records_by_build[build] = data[header.header_size:]
                    want_header = c.events_header(header.published_depth, header.tracked_depth,
                                                  header.max_instruments)
                    if feed == "A":
                        want_path = tmp / f"py_{build}.bin"
                        subprocess.run([sys.executable, str(REPO / "tools" / "intent_to_events.py"),
                                        str(intent), "-o", str(want_path),
                                        "--published-depth", str(header.published_depth),
                                        "--tracked-depth", str(header.tracked_depth),
                                        "--max-instruments", str(header.max_instruments)],
                                       check=True, stdout=subprocess.DEVNULL)
                        want_bytes = want_path.read_bytes()
                        if want_bytes != data:
                            _, want = c.read_events(want_bytes)
                            if want_bytes[:HEADER] != data[:HEADER]:
                                problems.append(f"{build}: file header differs")
                            problems += first_difference(want, got, build) or \
                                [f"{build}: bytes differ outside the records"]
                    else:
                        if header != want_header:
                            problems.append(f"{build}: header {header} != {want_header}")
                        _, want = expected_feed_b(intent, manifest, want_header)
                        w, g = per_channel(want), per_channel(got)
                        for ch in sorted(set(w) | set(g)):
                            problems += first_difference(w.get(ch, []), g.get(ch, []),
                                                         f"{build} channel {ch}")
                base_build = builds[0][0]
                for build, recs in records_by_build.items():
                    if recs != records_by_build[base_build]:
                        problems.append(f"records of build {build} differ from {base_build}")
                if problems:
                    failed += 1
                    print(f"MISMATCH {name}/{pcap.name}")
                    for p in problems[:12]:
                        print(f"    {p}")
                else:
                    n = len(records_by_build[base_build]) // c.EVENT.size
                    how = "byte-identical" if feed == "A" else "same records per channel"
                    print(f"OK {name}/{pcap.name}: {n} records, {how}, "
                          f"{len(builds)} builds ({', '.join(b for b, _ in builds)})")
    for note in reset_notes:
        print(f"note: SequenceReset/ChannelReset with an event open: {note}")
    print(f"{checked} build runs on {len(sets)} sets, {failed} files with differences")
    if checked == 0:
        print("error: nothing was checked", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
