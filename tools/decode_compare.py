#!/usr/bin/env python3
"""decode_compare: b3_decode's full decode against the answer keys.

What it does:
  1. Every generator set under a data folder, both feeds: runs
     `b3_decode --decode` on the feed's pcap and checks the result with
     compare_decode.py --strict (Feed A in wire order, Feed B in order within
     each channel; tools/b3intent.py gives each feed's view of intent.jsonl).
     Also checks that b3_decode reports no body faults and one decoded row
     per message.
  2. The schema set: the decode must show what that set is for, so the
     comparison above is not passing vacuously: rows of older versions with
     absent fields, rows with a blockLength above the schema's (extra root
     bytes skipped), and template 999 as a hex body.
  3. B3's sample packet (feed_generator/tests/fixtures/b3_sample_packet/):
     wrapped in Ethernet/IPv4/UDP into a pcap, decoded, and compared field
     for field with decoded.json.
  Prints one line per check and exits 1 on any failure, or if nothing was
  checked.

What it must not do:
  Import b3gen. The keys are intent.jsonl, manifest.json and decoded.json.

Usage:
  python3 tools/decode_compare.py --cpp cpp_baseline/build/release/b3_decode DATA_DIR
"""

import argparse
import io
import json
import struct
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import compare_decode  # noqa: E402
import b3intent        # noqa: E402

SAMPLE = REPO / "feed_generator" / "tests" / "fixtures" / "b3_sample_packet"
# spec/LAYOUTS.md section 6: blockLength of the templates the schema checks use.
SCHEMA_BLOCK_LENGTH = {5: 36, 50: 72}
UNKNOWN_TEMPLATE = 999


def run_decode(binary, pcap, out):
    res = subprocess.run([str(binary), "--decode", str(out), str(pcap)], check=True,
                         capture_output=True, text=True)
    return json.loads(res.stdout)


def check_feed(binary, folder, feed, tmp):
    """[problem] for one feed of one set."""
    pcap = folder / f"feed{feed}.pcap"
    out = Path(tmp) / f"decode_{feed}.jsonl"
    counts = run_decode(binary, pcap, out)
    problems = []
    if any(counts["body_faults"].values()):
        problems.append(f"body faults: {counts['body_faults']}")
    if counts["decoded_rows"] != counts["messages"]:
        problems.append(f"{counts['decoded_rows']} decoded rows for {counts['messages']} messages")
    text = io.StringIO()
    with redirect_stdout(text):
        status = compare_decode.main([str(out), str(folder / "intent.jsonl"), "--strict",
                                      "--feed", feed])
    if status != 0:
        problems += ["compare_decode: " + line for line in text.getvalue().splitlines()]
    return problems, out


def check_schema_rows(decode_path):
    """[problem] unless the schema set's decode shows each thing the set tests."""
    absent, extended, unknown = [], [], []
    for line in open(decode_path, encoding="utf-8"):
        r = json.loads(line)
        if r.get("absent"):
            absent.append((r["templateId"], r["version"], tuple(sorted(r["absent"]))))
        base = SCHEMA_BLOCK_LENGTH.get(r["templateId"])
        if base is not None and r["blockLength"] > base:
            extended.append((r["templateId"], r["version"], r["blockLength"]))
        if r["templateId"] == UNKNOWN_TEMPLATE:
            unknown.append(r["fields"])
    problems = []
    versions = {v for _, v, _ in absent}
    if not {14, 15} <= versions:
        problems.append(f"expected absent fields at versions 14 and 15, found {sorted(versions)}")
    if not extended:
        problems.append("no row with a blockLength above the schema's")
    if len(unknown) != 1 or set(unknown[0]) != {"body"} or not unknown[0]["body"]:
        problems.append(f"expected one template {UNKNOWN_TEMPLATE} row with a hex body, got {unknown}")
    summary = (f"{len(absent)} rows with absent fields {sorted(set(absent))}; "
               f"{len(extended)} extended rows {sorted(set(extended))}; "
               f"template {UNKNOWN_TEMPLATE} body of {len(unknown[0]['body']) // 2 if unknown else 0} bytes")
    return problems, summary


# ---- B3's sample packet ----
def sample_pcap(payload, path):
    """A one-frame nanosecond pcap carrying payload in UDP (checksums 0, unchecked)."""
    udp = struct.pack("!HHHH", 50000, 30055, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0x4000, 64, 17, 0,
                     bytes([10, 0, 0, 1]), bytes([233, 252, 8, 55]))
    frame = bytes.fromhex("01005e7c0837" "02b300000001" "0800") + ip + udp
    header = struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, 1)
    path.write_bytes(header + struct.pack("<IIII", 1, 0, len(frame), len(frame)) + frame)


def check_sample(binary, tmp):
    hex_text = (SAMPLE / "packet.hex").read_text()
    payload = bytes.fromhex(" ".join(ln for ln in hex_text.splitlines() if not ln.startswith("#")))
    want = json.loads((SAMPLE / "decoded.json").read_text())
    pcap = Path(tmp) / "sample.pcap"
    sample_pcap(payload, pcap)
    out = Path(tmp) / "sample.jsonl"
    run_decode(binary, pcap, out)
    rows = [json.loads(line) for line in open(out)]
    problems = []
    if len(rows) != len(want["messages"]):
        return [f"{len(rows)} rows for {len(want['messages'])} messages"]
    h = want["packet_header"]
    for i, (row, msg) in enumerate(zip(rows, want["messages"])):
        expect = {"channel": h["channelNumber"], "sequenceVersion": h["sequenceVersion"],
                  "sequenceNumber": h["sequenceNumber"], "sendingTime": h["sendingTime"],
                  "index": i, "templateId": msg["message_header"]["templateId"],
                  "blockLength": msg["message_header"]["blockLength"],
                  "version": msg["message_header"]["version"], "fields": msg["fields"]}
        for k, v in expect.items():
            if not compare_decode.same(v, row.get(k)):
                problems.append(f"message {i} ({msg['template']}) {k}: want {v!r}, got {row.get(k)!r}")
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
    checked = failed = skipped = xfailed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for folder in sets:
            name = folder.relative_to(root)
            reason = b3intent.expected_failure(b3intent.load_manifest(folder / "intent.jsonl"))
            for feed in ("A", "B"):
                if not (folder / f"feed{feed}.pcap").is_file():
                    print(f"SKIPPED {name}/feed{feed}.pcap: no such file in this set")
                    skipped += 1
                    continue
                checked += 1
                problems, out = check_feed(args.cpp, folder, feed, tmp)
                if not problems and folder.name == "schema" and feed == "A":
                    schema_problems, summary = check_schema_rows(out)
                    problems += schema_problems
                    if not schema_problems:
                        print(f"    schema set: {summary}")
                if problems and reason:
                    xfailed += 1
                    print(f"XFAIL {name}/feed{feed}.pcap: {reason} ({len(problems)} differences, "
                          f"first: {problems[0]})")
                elif reason:
                    failed += 1
                    print(f"XPASS {name}/feed{feed}.pcap: matches, but is expected to fail "
                          f"({reason}); remove it from b3intent.EXPECTED_FAILURES")
                elif problems:
                    failed += 1
                    print(f"MISMATCH {name}/feed{feed}.pcap")
                    for p in problems[:12]:
                        print(f"    {p}")
                else:
                    print(f"OK {name}/feed{feed}.pcap: strict match")
                out.unlink(missing_ok=True)
        checked += 1
        sample_problems = check_sample(args.cpp, tmp)
        if sample_problems:
            failed += 1
            print("MISMATCH B3 sample packet")
            for p in sample_problems:
                print(f"    {p}")
        else:
            print("OK B3 sample packet: matches decoded.json")
    print(f"{checked - failed - xfailed}/{checked} checks pass ({len(sets)} sets, sample packet), "
          f"{xfailed} expected failures, {skipped} skipped")
    if checked <= 1:
        print("error: no generator pcap was checked", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
