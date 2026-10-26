#!/usr/bin/env python3
"""pcap_crosscheck: the C++ pcap reader against an independent Python one.

What it does:
  For every feedA.pcap and feedB.pcap under a data folder, computes the same
  JSON line cpp_baseline/apps/b3_pcapinfo prints (counters, packets and
  payload bytes per destination group, CRC-32 over timestamp, length and
  payload of every datagram) with the feed generator's independent test
  reader (feed_generator/tests/pcap_reader.py, which never imports b3gen).
  Runs b3_pcapinfo on the same file and fails on any difference.

  The reference reader handles what the generator writes: nanosecond pcap,
  Ethernet without VLAN tags, IPv4 without options. A file outside that is
  reported as unsupported rather than guessed at.

What it must not do:
  Import b3gen, or reuse any C++ logic. Interpret UDP payloads.

Usage:
  python3 tools/pcap_crosscheck.py --cpp cpp_baseline/build/release/b3_pcapinfo DATA_DIR
  python3 tools/pcap_crosscheck.py --python-only FILE.pcap
"""

import argparse
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "feed_generator" / "tests"))

import pcap_reader  # noqa: E402  (independent reader, no b3gen)

PCAP_MAGIC_NS = 0xA1B23C4D
COUNTERS = ("frames", "udp_payloads", "payload_bytes", "non_ipv4", "non_udp", "fragments",
            "truncated", "bad_lengths", "trailing_garbage")


class Unsupported(Exception):
    """The reference reader cannot read this file faithfully."""


def python_info(path):
    data = Path(path).read_bytes()
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic != PCAP_MAGIC_NS:
        raise Unsupported(f"magic {magic:#010x}: the reference reader assumes ns, little-endian")
    try:
        _, records = pcap_reader.read_pcap(data)
    except (AssertionError, struct.error) as e:
        raise Unsupported(f"record cut off at end of file ({e})") from e

    counts = dict.fromkeys(COUNTERS, 0)
    groups = {}
    crc = 0
    for rec in records:
        counts["frames"] += 1
        if rec["incl_len"] < rec["orig_len"]:
            counts["truncated"] += 1
            continue
        if rec["ethertype"] == 0x8100:
            raise Unsupported("VLAN tag: the reference reader assumes none")
        if rec["ethertype"] != 0x0800 or rec["ver_ihl"] >> 4 != 4:
            counts["non_ipv4"] += 1
            continue
        if rec["ver_ihl"] & 0x0F != 5:
            raise Unsupported("IPv4 options: the reference reader assumes a 20-byte header")
        if rec["ip_total_length"] > rec["incl_len"] - 14:
            counts["bad_lengths"] += 1
            continue
        if rec["proto"] != 17:
            counts["non_udp"] += 1
            continue
        if rec["flags_frag"] & 0x3FFF:
            counts["fragments"] += 1
            continue
        if not 8 <= rec["udp_length"] <= rec["ip_total_length"] - 20:
            counts["bad_lengths"] += 1
            continue
        payload = rec["payload"]
        counts["udp_payloads"] += 1
        counts["payload_bytes"] += len(payload)
        key = f"{rec['dst_ip']}:{rec['dst_port']}"
        g = groups.setdefault(key, {"packets": 0, "bytes": 0})
        g["packets"] += 1
        g["bytes"] += len(payload)
        crc = zlib.crc32(struct.pack("<QI", rec["time_ns"], len(payload)), crc)
        crc = zlib.crc32(payload, crc)
    return {**counts, "crc32": crc, "groups": dict(sorted(groups.items(), key=group_order))}


def group_order(item):
    ip, port = item[0].rsplit(":", 1)
    return tuple(int(x) for x in ip.split(".")), int(port)


def cpp_info(binary, path):
    out = subprocess.run([str(binary), str(path)], check=True, capture_output=True, text=True)
    info = json.loads(out.stdout)
    info.pop("file")
    return info


def diff(want, got):
    out = []
    for k in sorted(set(want) | set(got)):
        if want.get(k) != got.get(k):
            out.append(f"{k}: python {want.get(k)!r}, c++ {got.get(k)!r}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("target", help="data folder (every feedA/feedB.pcap below it), or one .pcap")
    ap.add_argument("--cpp", help="path to b3_pcapinfo")
    ap.add_argument("--python-only", action="store_true", help="print the Python line only")
    args = ap.parse_args(argv)

    target = Path(args.target)
    files = [target] if target.is_file() else sorted(
        p for p in target.rglob("feed[AB].pcap"))
    if not files:
        print(f"error: no feedA.pcap or feedB.pcap under {target}", file=sys.stderr)
        return 2
    if args.python_only:
        for f in files:
            print(json.dumps({"file": str(f), **python_info(f)}, separators=(",", ":")))
        return 0
    if not args.cpp:
        ap.error("--cpp is required unless --python-only")

    failed = 0
    for f in files:
        name = f.relative_to(target) if f != target else f.name
        try:
            want = python_info(f)
        except Unsupported as e:
            print(f"UNSUPPORTED {name}: {e}")
            failed += 1
            continue
        got = cpp_info(args.cpp, f)
        problems = diff(want, got)
        if problems:
            failed += 1
            print(f"MISMATCH {name}")
            for p in problems:
                print(f"    {p}")
        else:
            print(f"OK {name}: {want['frames']} frames, {want['udp_payloads']} payloads, "
                  f"{want['payload_bytes']} bytes, {len(want['groups'])} groups")
    print(f"{len(files) - failed}/{len(files)} files match")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
