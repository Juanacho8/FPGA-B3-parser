#!/usr/bin/env python3
"""events_compare: b3_decode --events (port 1) against intent_to_events.py.

What it does:
  For every generator set under a data folder, both feeds, and every build
  given with --build (release, k16, k32, uncapped):
    * Feed A: the C++ events.bin must be byte-identical to
      tools/intent_to_events.py's, run with the build's N, K (0 = uncapped)
      and MAX_INSTRUMENTS, so the file headers agree too.
    * Feed B: byte-identical too, in full order: the converter follows
      Feed B's own packet order from its pcap (tools/b3feed.py), the same
      way tools/expected_book.py does for port 2.
    * Across builds: the records (everything after the header) must be the
      same in every build; only the header's N and K may differ.
  The first difference is reported with its packet and message position, in
  dump_events.py's format. The C++'s counts of SequenceReset/ChannelReset
  arriving with an event open are reported (DESIGN.md 3.5 does not close an
  event on them). Sets in b3intent.EXPECTED_FAILURES are reported XFAIL
  when they differ, and fail as XPASS when they match.

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
import b3replay             # noqa: E402
import gen_instruments      # noqa: E402

HEADER = c.FILE_HEADER.size
_TABLE = []


def table_path():
    """The generator config's instrument table as a file, written once: the
    table the converter uses by default, given to the C++ too."""
    if not _TABLE:
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        f.write(gen_instruments.table_text(REPO / "feed_generator" / "config.toml"))
        f.close()
        _TABLE.append(f.name)
    return _TABLE[0]


def run_cpp(binary, pcap, out):
    res = subprocess.run([str(binary), "--instruments", table_path(), "--events", str(out), str(pcap)],
                         check=True, capture_output=True, text=True)
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
    checked = failed = xfailed = 0
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
                # The answer key, in this feed's own packet order (tools/b3feed.py).
                try:
                    want_records = intent_to_events.convert_feed(
                        intent, manifest, feed, pcap, b3replay.read_table(table_path()))
                    want_error = None
                except b3replay.Broken as e:
                    want_records, want_error = None, e
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
                    if want_records is None:
                        problems.append(f"{build}: answer key: {want_error}")
                        continue
                    want_bytes = want_header.pack() + b"".join(e.pack() for e in want_records)
                    if want_bytes != data:
                        if want_bytes[:HEADER] != data[:HEADER]:
                            problems.append(f"{build}: file header differs")
                        problems += first_difference(want_records, got, build) or \
                            [f"{build}: bytes differ outside the records"]
                base_build = builds[0][0]
                for build, recs in records_by_build.items():
                    if recs != records_by_build[base_build]:
                        problems.append(f"records of build {build} differ from {base_build}")
                reason = b3intent.expected_failure(manifest)
                if problems and reason:
                    xfailed += 1
                    print(f"XFAIL {name}/{pcap.name}: {reason} ({len(problems)} differences, "
                          f"first: {problems[0]})")
                elif reason:
                    failed += 1
                    print(f"XPASS {name}/{pcap.name}: matches, but is expected to fail ({reason}); "
                          f"remove it from b3intent.EXPECTED_FAILURES")
                elif problems:
                    failed += 1
                    print(f"MISMATCH {name}/{pcap.name}")
                    for p in problems[:12]:
                        print(f"    {p}")
                else:
                    n = len(records_by_build[base_build]) // c.EVENT.size
                    print(f"OK {name}/{pcap.name}: {n} records, byte-identical in full order, "
                          f"{len(builds)} builds ({', '.join(b for b, _ in builds)})")
    for note in reset_notes:
        print(f"note: SequenceReset/ChannelReset with an event open: {note}")
    print(f"{checked} build runs on {len(sets)} sets, {failed} files with differences, "
          f"{xfailed} expected failures")
    if checked == 0:
        print("error: nothing was checked", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
