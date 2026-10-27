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

  Guards, checked before the C++ output is looked at:
    * fault positions: the packet count comes from "rows with index 0",
      which needs every fault to leave message 0 standing (guard 1)
    * packet count: that same count from the pcap, read by the generator's
      independent reader, must agree (guard 2)
    * manifest shape and fault kinds: b3intent.validate_manifest, plus every
      kind mapped to a walker reason here (guard 3)

What it must not do:
  Import b3gen. The answer keys come from the generator's intent log and
  manifest; the pcap is read only by guard 2, with the generator's
  independent reader (feed_generator/tests/pcap_reader.py), never by C++ code.

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

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "feed_generator" / "tests"))

import b3intent  # noqa: E402
import pcap_reader  # noqa: E402  (independent of b3gen and of the C++)

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
PACKET_HEADER = 16
# b3_decode's per-template counts hold ids 0..255 (walker.hpp CountingSink);
# larger ids are only in unknown_templates and the listing.
TEMPLATE_SLOTS = 256

# Guard 3: every fault kind b3intent accepts must map to a walker reason.
_unmapped = set(b3intent.KNOWN_FAULT_KINDS) - {"short"} - set(FAULT_REASON)
if _unmapped:
    raise SystemExit(f"decode_crosscheck: no walker reason for fault kinds {sorted(_unmapped)}")


class Unsupported(Exception):
    pass


class AssumptionBroken(Exception):
    """A fact the expected counts rely on no longer holds for this data."""


def check_fault_positions(manifest, where):
    """Guard 1. expected() counts packets as rows with index 0. That holds
    only if every packet keeps its message 0 in the intent view, so a fault
    must leave message 0 standing: messages_before >= 1. It holds today
    because b3gen/scenarios.py malform() faults message 1, or the last message
    for "truncated", and only in packets with at least 3 messages. Short
    datagrams have no packet header and no rows, so they are exempt."""
    faults = manifest.get("faults") if isinstance(manifest, dict) else None
    if not isinstance(faults, list):
        return
    for i, fault in enumerate(faults):
        if not isinstance(fault, dict) or fault.get("fault") == "short":
            continue
        before = fault.get("messages_before")
        if before is None or before == 0:
            raise AssumptionBroken(
                f"{where}: faults[{i}] ({fault.get('fault')!r} at "
                f"{[fault.get(k) for k in b3intent.PACKET_FIELDS]}) has messages_before "
                f"{'missing' if before is None else 0}: the intent log can no longer give the "
                f"packet count (rows with index 0), since that packet keeps no message 0")


def pcap_packet_count(pcap):
    """Guard 2's second source: datagrams of at least 16 bytes in the pcap,
    read with the generator's independent reader."""
    _, records = pcap_reader.read_pcap(Path(pcap).read_bytes())
    return sum(1 for rec in records if len(rec["payload"]) >= PACKET_HEADER)


def check_packet_count(intent_packets, pcap, where):
    """Guard 2: the intent log's packet count against the pcap's, without the C++."""
    on_wire = pcap_packet_count(pcap)
    if on_wire != intent_packets:
        raise AssumptionBroken(
            f"{where}: the intent log gives {intent_packets} packets (rows with index 0) but "
            f"the pcap has {on_wire} datagrams of at least 16 bytes: the 'one row at index 0 "
            f"per packet' assumption broke (an empty packet, or a fault at message 0?)")


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
              "msg_index_overflow": 0,
              "malformed_packets": sum(malformed.values()), "malformed": malformed,
              "templates": {str(t): n for t, n in sorted(templates.items()) if t < TEMPLATE_SLOTS}}
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
        try:
            raw = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            check_fault_positions(raw, f"{name}/manifest.json")              # guard 1
            manifest = b3intent.validate_manifest(raw, f"{name}/manifest.json")  # guard 3
        except (AssumptionBroken, b3intent.ManifestError) as e:
            print(f"FAIL {e}")
            failed += 1
            checked += 1
            continue
        for feed in FEEDS:
            pcap = folder / f"feed{feed}.pcap"
            if not pcap.is_file():
                print(f"SKIPPED {name}/{pcap.name}: no such file in this set")
                skipped += 1
                continue
            checked += 1
            try:
                want_counts, want_list = expected(folder / "intent.jsonl", manifest, feed)
                check_packet_count(want_counts["packets"], pcap, f"{name}/{pcap.name}")  # guard 2
            except Unsupported as e:
                print(f"UNSUPPORTED {name}/{pcap.name}: {e}")
                failed += 1
                continue
            except AssumptionBroken as e:
                print(f"FAIL {e}")
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
