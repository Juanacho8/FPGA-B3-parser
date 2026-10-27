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
      jump marks one; EmptyBook and ChannelReset clear. Each instrument's
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

import b3intent           # noqa: E402
import decode_crosscheck  # noqa: E402
import gen_instruments    # noqa: E402

SEQUENCE_RESET, SEQUENCE, EMPTY_BOOK, CHANNEL_RESET = 1, 2, 9, 11
MAX_VERSION = 65534
BODY_FAULTS = ("group_header_past_end", "group_entry_past_end", "var_data_length_past_end",
               "var_data_past_end")


class Broken(Exception):
    """The answer keys cannot give an expected value; never guessed."""


def build_params():
    """book_params.hpp's defaults: what the default (release) build uses."""
    text = (REPO / "cpp_baseline" / "include" / "b3" / "book_params.hpp").read_text()
    value = {name: int(v) for name, v in re.findall(r"^#define (B3_[A-Z_]+) (\d+)$", text, re.M)}
    return {"published_depth": value["B3_PUBLISHED_DEPTH"], "tracked_depth": value["B3_TRACKED_DEPTH"],
            "max_instruments": value["B3_MAX_INSTRUMENTS"],
            "book": "uncapped" if value["B3_BOOK_UNCAPPED"] else "capped"}


def read_table(path):
    """[(securityID, channel)] from an instrument table file."""
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].split()
        if line:
            out.append((int(line[0]), int(line[1])))
    return out


def check_unreliable_shape(manifest, where):
    books = manifest.get("unreliable_books")
    if not isinstance(books, dict) or not all(
            isinstance(k, str) and isinstance(v, list) and len(v) == 3 and all(isinstance(x, int) for x in v)
            for k, v in books.items()):
        raise b3intent.ManifestError(f"{where}: 'unreliable_books' must be {{instrument: [channel, "
                                     f"sequenceVersion, sequenceNumber]}}, got {books!r:.80}")
    return books


def packets(rows):
    """Group a feed's rows into packets (each starts at index 0)."""
    current = []
    for row in rows:
        if row["index"] == 0 and current:
            yield current
            current = []
        current.append(row)
    if current:
        yield current


class Replay:
    """The sequencer and tracker rules over the answer keys' rows."""

    def __init__(self, table, manifest, feed):
        self.table = table
        self.slot = {sid: i for i, (sid, _) in enumerate(table)}
        self.missing = {tuple(k) for k in manifest["missing"][feed]}
        self.faulted = {(f["channel"], f["sequenceVersion"], f["sequenceNumber"])
                        for f in manifest.get("faults") or [] if f["fault"] != "short"}
        self.ch = {}   # channel -> {"seen", "reset", "version", "next"}
        self.seq = dict(sequenced=0, gaps=0, gap_events=0, heartbeats=0, heartbeat_mismatches=0,
                        resets=0, reset_mismatches=0, version_changes=0, seq_reset_not_first=0,
                        heartbeat_without_sequence=0,
                        late_after_gap=0)   # the view never holds a late packet: only 0 is derivable
        self.msg_before_rebuild = 0
        self.losses = 0
        self.unknown = 0
        self.state = [dict(have=False, last=0, stale=False, first=None, gaps=0, events=0, rebuild=False)
                      for _ in table]
        self.gap_packets = set()

    # ---- sequencer ----
    def packet(self, rows):
        first = rows[0]
        ch, ver, num = first["channel"], first["sequenceVersion"], first["sequenceNumber"]
        c = self.ch.setdefault(ch, dict(seen=False, reset=False, version=0, next=0))
        if num == 0:
            self.seq["heartbeats"] += 1
            if first["templateId"] != SEQUENCE:
                self.seq["heartbeat_without_sequence"] += 1
        else:
            self.seq["sequenced"] += 1
            if first["templateId"] == SEQUENCE_RESET:
                self.seq["resets"] += 1
                c["reset"] = True
            else:
                self.sequence(c, ch, ver, num)
        for row in rows:
            self.message(row, c)
        if (ch, ver, num) in self.faulted:
            self.loss(ch, (ch, ver, num))   # discarded malformed tail, after its messages

    def sequence(self, c, ch, ver, num):
        if c["reset"]:
            c["reset"] = False
            want = ver if not c["seen"] else (1 if c["version"] >= MAX_VERSION else c["version"] + 1)
            if (ver, num) != (want, 1):
                self.seq["reset_mismatches"] += 1
            c.update(seen=True, version=want, next=1)
        if not c["seen"]:
            c.update(seen=True, version=ver, next=num + 1)
        elif ver != c["version"]:
            self.seq["version_changes"] += 1
            c.update(version=ver, next=num + 1)
        elif num == c["next"]:
            c["next"] += 1
        elif num > c["next"]:
            lost = {(ch, ver, n) for n in range(c["next"], num)}
            if not lost <= self.missing:
                raise Broken(f"gap {sorted(lost - self.missing)} is not in the manifest's missing list")
            self.gap_packets |= lost
            self.seq["gaps"] += num - c["next"]
            self.seq["gap_events"] += 1
            self.loss(ch, (ch, ver, c["next"]))
            c["next"] = num + 1
        else:
            raise Broken(f"duplicate packet {(ch, ver, num)} in a single feed's intent view")

    # ---- tracker ----
    def loss(self, ch, point):
        self.losses += 1
        for slot, (_, tch) in enumerate(self.table):
            if tch == ch:
                self.stale(slot, point)

    def stale(self, slot, point):
        st = self.state[slot]
        if not st["stale"]:
            st["events"] += 1
        st["stale"] = True
        if st["first"] is None:
            st["first"] = list(point)

    def message(self, row, c):
        t, f = row["templateId"], row["fields"]
        ch = row["channel"]
        if t == SEQUENCE and row["sequenceNumber"] == 0:
            if c["seen"] or c["reset"]:
                expected = 1 if c["reset"] else c["next"]
                if f.get("nextSeqNo") != expected:
                    self.seq["heartbeat_mismatches"] += 1
        elif t == SEQUENCE_RESET and row["index"] > 0:
            self.seq["seq_reset_not_first"] += 1
        elif t == CHANNEL_RESET:
            for slot, (_, tch) in enumerate(self.table):
                if tch == ch:
                    self.state[slot].update(stale=False, rebuild=True)
        sid = f.get("securityID")
        if sid is None or "securityID" in row["null"]:
            return
        slot = self.slot.get(sid)
        if slot is None:
            self.unknown += 1
            return
        st = self.state[slot]
        if t == EMPTY_BOOK:
            st.update(stale=False, have=True, last=0, rebuild=False)
            return
        if st["rebuild"]:
            self.msg_before_rebuild += 1   # after a ChannelReset, before its EmptyBook (open item J)
        if "rptSeq" in f and "rptSeq" not in row.get("absent", []):
            if st["have"] and f["rptSeq"] != st["last"] + 1:
                st["gaps"] += 1
                self.stale(slot, (ch, row["sequenceVersion"], row["sequenceNumber"]))
            st.update(have=True, last=f["rptSeq"])


def expected_counters(folder, manifest, feed, table):
    counts, _ = decode_crosscheck.expected(folder / "intent.jsonl", manifest, feed)
    rows = [r for _, r in b3intent.feed_rows(folder / "intent.jsonl", manifest, feed)]
    replay = Replay(table, manifest, feed)
    for pkt in packets(rows):
        replay.packet(pkt)
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
