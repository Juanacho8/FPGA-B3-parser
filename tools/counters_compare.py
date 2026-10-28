#!/usr/bin/env python3
"""counters_compare: b3_decode --counters against counters derived from the answer keys.

What it does:
  For every generator set under a data folder and both feeds, runs
  `b3_decode --instruments T --counters C` (T written from config.toml by
  tools/gen_instruments.py) and compares C with counters derived here,
  without the C++, from manifest.json and the feed's view of intent.jsonl
  (tools/b3intent.py):
    * packets, messages per template, malformed by reason, short datagrams,
      msg_index overflow (as tools/decode_crosscheck.py derives them)
    * sequencing (docs/DESIGN.md 3.6.3), replayed over the feed's packets:
      gaps, duplicates, heartbeats and their nextSeqNo, resets
    * the tracker (DESIGN.md 3.6.4), replayed over the feed's messages:
      rptSeq gaps per instrument, unknown instruments, and stale flags: a gap
      (at its first missing number) or a discarded malformed tail (at the
      faulted packet) marks every table instrument of the channel; an rptSeq
      jump marks one; a channel silent past the silence limit marks the
      channel (capture times from the feed's pcap, tools/b3feed.py);
      EmptyBook and ChannelReset clear. Each instrument's
      first stale point, stale events and state at the end are compared.
    * the build parameters (N, K, MAX_INSTRUMENTS, book) against
      cpp_baseline/include/b3/book_params.hpp's defaults (the release build)
  Instrument names map to securityIDs through the intent rows, never the C++.
  Every gap the replay finds must be packets the manifest lists as missing
  from this feed; missing packets after a channel's last packet are reported
  as undetectable (nothing follows them).
  Cross-check: every instrument in the manifest's unreliable_books must be
  flagged stale at its point or earlier (on the same channel).
  Late packets (manifest reordered, reorder scenario): the replay runs in
  the reorder window's release order (tools/b3intent.py), so it expects no
  gap, no duplicate and no stale book, and "reordered" = the manifest's
  late packets for the feed, [channel, sequenceVersion, sequenceNumber] in
  arrival order. That key is expected only where the list is not empty,
  until b3_decode reports it on every file (the reorder window).
  Sets in b3intent.EXPECTED_FAILURES are reported XFAIL when they differ,
  and fail as XPASS when they match.
  Fails if a manifest key read here is missing or changes shape, or if
  nothing was checked.

What it must not do:
  Import b3gen, or use the C++ output to compute what it expects.

Usage:
  python3 tools/counters_compare.py --cpp cpp_baseline/build/release/b3_decode DATA_DIR
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import b3feed             # noqa: E402
import b3intent           # noqa: E402
import decode_crosscheck  # noqa: E402
import gen_instruments    # noqa: E402

from b3replay import Broken, Replay, packets, read_table  # noqa: E402,F401  (shared with intent_to_events)
BODY_FAULTS = ("group_header_past_end", "group_entry_past_end", "var_data_length_past_end",
               "var_data_past_end")


def build_params():
    """book_params.hpp's defaults: what the default (release) build uses."""
    text = (REPO / "cpp_baseline" / "include" / "b3" / "book_params.hpp").read_text()
    value = {name: int(v) for name, v in re.findall(r"^#define (B3_[A-Z_]+) (\d+)$", text, re.M)}
    return {"published_depth": value["B3_PUBLISHED_DEPTH"], "tracked_depth": value["B3_TRACKED_DEPTH"],
            "max_instruments": value["B3_MAX_INSTRUMENTS"],
            "book": "uncapped" if value["B3_BOOK_UNCAPPED"] else "capped"}


def check_unreliable_shape(manifest, where):
    books = manifest.get("unreliable_books")
    if not isinstance(books, dict) or not all(
            isinstance(k, str) and isinstance(v, list) and len(v) == 3 and all(isinstance(x, int) for x in v)
            for k, v in books.items()):
        raise b3intent.ManifestError(f"{where}: 'unreliable_books' must be {{instrument: [channel, "
                                     f"sequenceVersion, sequenceNumber]}}, got {books!r:.80}")
    return books


def expected_counters(folder, manifest, feed, table):
    counts, _ = decode_crosscheck.expected(folder / "intent.jsonl", manifest, feed)
    rows = [r for _, r in b3intent.feed_rows(folder / "intent.jsonl", manifest, feed)]
    replay = Replay(table, manifest, feed)
    if b3intent.expected_failure(manifest):
        # The key of a decoder feature not built yet (the reorder window): the
        # view's order (the window's release order), without capture times.
        for pkt in packets(rows):
            replay.packet(pkt)
    else:
        # The feed's own packet order and capture times: the silence rule.
        b3feed.drive(folder / "intent.jsonl", manifest, feed, folder / f"feed{feed}.pcap", replay)
    last_seen = {}
    for r in rows:
        if r["sequenceNumber"]:
            last_seen[r["channel"]] = max(last_seen.get(r["channel"], (0, 0)),
                                          (r["sequenceVersion"], r["sequenceNumber"]))
    undetectable = sorted(k for k in replay.missing - replay.gap_packets
                          if (k[1], k[2]) > last_seen.get(k[0], (0, 0)))
    unexplained = sorted(replay.missing - replay.gap_packets - set(undetectable))
    if unexplained:
        raise Broken(f"missing packets {unexplained[:5]} neither in a gap nor after the last packet")
    exp = {k: counts[k] for k in ("datagrams", "short_datagrams", "packets", "oversize_packets",
                                  "empty_packets", "messages", "unknown_templates",
                                  "msg_index_overflow", "malformed_packets", "malformed", "templates")}
    exp.update(params=build_params(), duplicates=0, sequencing=replay.seq, losses=replay.losses,
               silences=replay.silences,
               unknown_instrument_messages=replay.unknown, msg_before_rebuild=replay.msg_before_rebuild,
               body_faults=dict.fromkeys(BODY_FAULTS, 0))
    late = [r["packet"] for r in (manifest.get("reordered") or {}).get(feed, [])]
    if late:
        exp["reordered"] = late
    exp["instruments"] = {
        str(sid): {"slot": slot, "channel": ch, "rpt_seq_gaps": st["gaps"], "stale_events": st["events"],
                   "stale_at_end": st["stale"], "first_stale": st["first"]}
        for slot, ((sid, ch), st) in enumerate(zip(table, replay.state))}
    return exp, undetectable


def names_to_ids(intent):
    out = {}
    for _, r in b3intent.read_jsonl(intent):
        sid = r["fields"].get("securityID")
        if r.get("instrument") and sid is not None:
            if out.setdefault(r["instrument"], sid) != sid:
                raise Broken(f"instrument {r['instrument']} has two securityIDs in the intent log")
    return out


def check_unreliable(books, ids, got, feed_missing):
    """[problem] for instruments of unreliable_books not flagged at their point or earlier."""
    problems = []
    for name, (ch, ver, num) in books.items():
        if name not in ids:
            problems.append(f"unreliable_books names {name}, which the intent log never mentions")
            continue
        flagged = got["instruments"].get(str(ids[name]), {}).get("first_stale")
        if flagged is None or flagged[0] != ch or (flagged[1], flagged[2]) > (ver, num):
            problems.append(f"unreliable_books: {name} unreliable from {[ch, ver, num]}, "
                            f"decoder flags it at {flagged}")
    return problems


def diff(want, got, path=""):
    if isinstance(want, dict) and isinstance(got, dict):
        out = []
        for k in want:
            if k not in got:
                out.append(f"{path}{k}: missing from the C++ counters")
            else:
                out += diff(want[k], got[k], f"{path}{k}.")
        return out
    return [] if want == got else [f"{path.rstrip('.')}: keys {want!r}, c++ {got!r}"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data", help="generator output folder (make data)")
    ap.add_argument("--cpp", required=True, help="path to b3_decode")
    ap.add_argument("--config", default=str(REPO / "feed_generator" / "config.toml"))
    args = ap.parse_args(argv)

    root = Path(args.data)
    sets = sorted(p.parent for p in root.rglob("intent.jsonl"))
    if not sets:
        print(f"error: no generator sets under {root}: run make data", file=sys.stderr)
        return 2
    checked = failed = skipped = xfailed = 0
    with tempfile.TemporaryDirectory() as tmp:
        table_path = Path(tmp) / "instruments.txt"
        table_path.write_text(gen_instruments.table_text(args.config))
        table = read_table(table_path)
        for folder in sets:
            name = folder.relative_to(root)
            try:
                manifest = b3intent.load_manifest(folder / "intent.jsonl")
                books = check_unreliable_shape(manifest, f"{name}/manifest.json")
                ids = names_to_ids(folder / "intent.jsonl")
            except (b3intent.ManifestError, Broken) as e:
                print(f"FAIL {e}")
                failed += 1
                checked += 1
                continue
            for sid, _ in table:
                if sid not in ids.values():
                    print(f"note {name}: table securityID {sid} never appears in the intent log")
            for feed in ("A", "B"):
                pcap = folder / f"feed{feed}.pcap"
                if not pcap.is_file():
                    print(f"SKIPPED {name}/{pcap.name}: no such file in this set")
                    skipped += 1
                    continue
                checked += 1
                try:
                    want, undetectable = expected_counters(folder, manifest, feed, table)
                except Broken as e:
                    print(f"FAIL {name}/{pcap.name}: {e}")
                    failed += 1
                    continue
                out = Path(tmp) / "counters.json"
                subprocess.run([str(args.cpp), "--instruments", str(table_path), "--counters", str(out),
                                str(pcap)], check=True, capture_output=True)
                got = json.loads(out.read_text())
                problems = diff(want, got) + check_unreliable(books, ids, got, manifest["missing"][feed])
                stale = {k: v["first_stale"] for k, v in want["instruments"].items() if v["first_stale"]}
                note = f"; {len(undetectable)} missing packets after the channel's last (undetectable)" \
                    if undetectable else ""
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
                    print(f"OK {name}/{pcap.name}: gaps {want['sequencing']['gaps']}, losses "
                          f"{want['losses']}, stale {stale or '{}'}{note}")
    print(f"{checked - failed - xfailed}/{checked} files match in {len(sets)} sets, "
          f"{xfailed} expected failures, {skipped} skipped")
    if checked == 0:
        print("error: nothing was checked", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
