#!/usr/bin/env python3
"""decode_crosscheck: b3_decode's packet decoder and walker against the answer keys.

What it does:
  For every generator set under a data folder and both feeds, runs
  cpp_baseline's b3_decode (--list) on the feed's pcap and compares with what
  the intent log and manifest say a correct decoder of that feed sees
  (tools/b3intent.py):
    * messages per templateId, unknown templates, packets, datagrams
    * short datagrams and malformed packets by reason, against manifest faults
    * the per-message listing (channel, sequenceVersion, sequenceNumber,
      msg_index, templateId, messageLength): Feed A in full wire order; Feed B
      in order within each channel, since Feed B's cross-channel order is not
      in the intent log (docs/DESIGN.md 3.7)
  messageLength is not in intent.jsonl, so it is computed from the row:
  12 + blockLength + var data (2-byte length prefix + bytes) for News, the
  only emitted template with var data. A row of a template with groups is
  reported as unsupported.
  A set missing a feed's pcap is reported as SKIPPED.

What it must not do:
  Import b3gen, or read the pcap itself. The answer keys come from the
  generator's intent log and manifest only.

Usage:
  python3 tools/decode_crosscheck.py --cpp cpp_baseline/build/release/b3_decode DATA_DIR
"""

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import b3intent  # noqa: E402

# spec/LAYOUTS.md section 6: every templateId of the schema.
SCHEMA_TEMPLATES = {1, 2, 3, 5, 9, 10, 11, 12, 15, 16, 17, 19, 21, 22, 24, 25, 27, 28, 29, 30,
                    50, 51, 52, 53, 54, 55, 56, 57, 71}
WITH_GROUPS = {12, 71}
# spec/LAYOUTS.md 7, News_5: three var data fields, each a uint16 length then bytes.
VAR_DATA = {5: ("headline", "text", "uRLLink")}
MESSAGE_HEADER = 12

# manifest fault kind -> b3_decode malformed reason (DESIGN.md 3.6.1)
FAULT_REASON = {"truncated": "header_truncated", "length_zero": "length_zero",
                "length_too_small": "length_too_small", "length_past_end": "length_past_end",
                "bad_encoding": "bad_encoding"}
REASONS = ("header_truncated", "length_zero", "length_too_small", "length_past_end",
           "bad_encoding", "bad_schema_id", "block_length_past_end")
FEEDS = ("A", "B")


class Unsupported(Exception):
    pass


def message_length(row):
    t = row["templateId"]
    if t in WITH_GROUPS:
        raise Unsupported(f"template {t} has groups; its messageLength is not in the intent log")
    length = MESSAGE_HEADER + row["blockLength"]
    for name in VAR_DATA.get(t, ()):
        length += 2 + len(row["fields"][name].encode("utf-8"))
    return length


def expected(intent, manifest, feed):
    """(counts dict, listing [tuple]) a correct decoder of feed sees."""
    listing, templates = [], Counter()
    packets = 0
    for _, row in b3intent.feed_rows(intent, manifest, feed):
        listing.append((*b3intent.row_key(row), row["templateId"], message_length(row)))
        templates[row["templateId"]] += 1
        packets += row["index"] == 0
    missing = {tuple(k) for k in manifest.get("missing", {}).get(feed, [])}
    malformed = dict.fromkeys(REASONS, 0)
    short = 0
    for fault in manifest.get("faults") or []:
        if fault["fault"] == "short":
            short += 1
        elif (fault["channel"], fault["sequenceVersion"], fault["sequenceNumber"]) not in missing:
            malformed[FAULT_REASON[fault["fault"]]] += 1
    counts = {"datagrams": packets + short, "short_datagrams": short, "packets": packets,
              "oversize_packets": 0, "empty_packets": 0, "messages": len(listing),
              "unknown_templates": sum(n for t, n in templates.items() if t not in SCHEMA_TEMPLATES),
              "malformed_packets": sum(malformed.values()), "malformed": malformed,
              "templates": {str(t): n for t, n in sorted(templates.items())}}
    return counts, listing


def run_cpp(binary, pcap):
    with tempfile.NamedTemporaryFile("r", suffix=".txt") as tmp:
        out = subprocess.run([str(binary), "--list", tmp.name, str(pcap)], check=True,
                             capture_output=True, text=True)
        listing = [tuple(int(x) for x in line.split()) for line in open(tmp.name)]
    counts = json.loads(out.stdout)
    counts.pop("file")
    return counts, listing


def per_channel(listing):
    out = {}
    for entry in listing:
        out.setdefault(entry[0], []).append(entry)
    return out


def first_difference(want, got):
    for i, (w, g) in enumerate(zip(want, got)):
        if w != g:
            return f"entry {i}: want {w}, got {g}"
    if len(want) != len(got):
        return f"length: want {len(want)}, got {len(got)}"
    return None


def compare(want_counts, want_list, got_counts, got_list, feed):
    problems = [f"{k}: keys {want_counts.get(k)!r}, c++ {got_counts.get(k)!r}"
                for k in sorted(set(want_counts) | set(got_counts))
                if want_counts.get(k) != got_counts.get(k)]
    if feed == "A":
        d = first_difference(want_list, got_list)
        if d:
            problems.append(f"listing: {d}")
    else:
        w, g = per_channel(want_list), per_channel(got_list)
        for ch in sorted(set(w) | set(g)):
            d = first_difference(w.get(ch, []), g.get(ch, []))
            if d:
                problems.append(f"listing, channel {ch}: {d}")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data", help="generator output folder (make data)")
    ap.add_argument("--cpp", required=True, help="path to b3_decode")
    args = ap.parse_args(argv)

    root = Path(args.data)
    sets = sorted(p.parent for p in root.rglob("intent.jsonl"))
    if not sets:
        print(f"error: no generator sets under {root}: run make data", file=sys.stderr)
        return 2
    checked = failed = skipped = 0
    for folder in sets:
        name = folder.relative_to(root)
        manifest = b3intent.load_manifest(folder / "intent.jsonl")
        for feed in FEEDS:
            pcap = folder / f"feed{feed}.pcap"
            if not pcap.is_file():
                print(f"SKIPPED {name}/{pcap.name}: no such file in this set")
                skipped += 1
                continue
            checked += 1
            try:
                want_counts, want_list = expected(folder / "intent.jsonl", manifest, feed)
            except Unsupported as e:
                print(f"UNSUPPORTED {name}/{pcap.name}: {e}")
                failed += 1
                continue
            got_counts, got_list = run_cpp(args.cpp, pcap)
            problems = compare(want_counts, want_list, got_counts, got_list, feed)
            if problems:
                failed += 1
                print(f"MISMATCH {name}/{pcap.name}")
                for p in problems:
                    print(f"    {p}")
            else:
                bad = want_counts["malformed_packets"]
                print(f"OK {name}/{pcap.name}: {want_counts['packets']} packets, "
                      f"{want_counts['messages']} messages"
                      + (f", {bad} malformed, {want_counts['short_datagrams']} short" if bad else "")
                      + (f", {want_counts['unknown_templates']} unknown"
                         if want_counts["unknown_templates"] else ""))
    print(f"{checked - failed}/{checked} files match in {len(sets)} sets, {skipped} skipped")
    if checked == 0:
        print("error: no pcap was checked", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
