#!/usr/bin/env python3
"""compare_decode: compares a decoder's decode.jsonl with intent.jsonl.

What it does:
  Builds the rows a correct decoder of one feed sees (tools/b3intent.py:
  lost packets and discarded malformed tails left out), then checks the
  decode row for row, field for field:
    * placement: channel, sequenceVersion, sequenceNumber, index
    * sendingTime, templateId, blockLength, version
    * fields: same names, same values, same JSON types (1 is not "1" or 1.0)
    * null and absent, as sets: a null field and an absent field are
      different answers ("absent" missing from a row means none)
    * news (the reassembled text on a News's last part): compared when the
      decode row has it; --strict requires it wherever intent.jsonl has it
    * instrument: compared only when the decode row has it, never required.
      It is a name from the generator's config, which a decoder of the wire
      does not read
    * no other keys: a key the decode row has and intent.jsonl does not
      (body_fault, undecoded, ...) is a mismatch
  Rows are matched per channel, in order. That is the order both feeds keep,
  so the same check works for a decode of Feed A or Feed B. Heartbeats
  (sequenceNumber 0, index 0) are matched in order too. For Feed A, whose
  wire order intent.jsonl follows, the order across channels is checked too.
  Prints the first mismatches with packet and message position, and exits
  1 if there are any.

What it must not do:
  Import b3gen or read the pcap. Treat the decode as the answer: the intent
  log is the key.

Usage:
  python3 tools/compare_decode.py decode.jsonl data/small/intent.jsonl [--feed A]
"""

import argparse
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import b3intent   # noqa: E402

HEADER_KEYS = ("sendingTime", "templateId", "blockLength", "version")
PLACEMENT_KEYS = ("channel", "sequenceVersion", "sequenceNumber", "index")
CONFIG_KEYS = ("instrument",)    # from the generator's config: never required
REASSEMBLED_KEYS = ("news",)     # required by --strict


def same(a, b):
    """Equal values with equal JSON types (bool, int, float, str, ...)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def diff_rows(want, got, strict=False):
    """Differences between an intent row and a decode row: [str]."""
    out = []
    for k in HEADER_KEYS:
        if k not in got:
            out.append(f"{k}: missing from decode")
        elif not same(want[k], got[k]):
            out.append(f"{k}: want {want[k]!r}, got {got[k]!r}")
    wf, gf = want["fields"], got.get("fields")
    if not isinstance(gf, dict):
        out.append("fields: missing from decode")
        gf = {}
    for name in wf:
        if name not in gf:
            out.append(f"fields.{name}: missing from decode (want {wf[name]!r})")
        elif not same(wf[name], gf[name]):
            out.append(f"fields.{name}: want {wf[name]!r}, got {gf[name]!r}")
    for name in gf:
        if name not in wf:
            out.append(f"fields.{name}: not in intent (got {gf[name]!r})")
    for k in ("null", "absent"):
        w, g = set(want.get(k, [])), set(got.get(k, []))
        if w != g:
            parts = []
            if w - g:
                parts.append(f"missing {sorted(w - g)}")
            if g - w:
                parts.append(f"extra {sorted(g - w)}")
            out.append(f"{k}: {', '.join(parts)}")
    if "null" not in got:
        out.append("null: missing from decode")
    for k in CONFIG_KEYS:
        if k in got and not same(want.get(k), got[k]):
            out.append(f"{k}: want {want.get(k)!r}, got {got[k]!r}")
    for k in REASSEMBLED_KEYS:
        if k in got or (strict and k in want):
            if k not in got:
                out.append(f"{k}: missing from decode")
            elif not same(want.get(k), got[k]):
                out.append(f"{k}: want {want.get(k)!r}, got {got[k]!r}")
    known = set(PLACEMENT_KEYS) | set(HEADER_KEYS) | {"fields", "null", "absent"} | \
        set(CONFIG_KEYS) | set(REASSEMBLED_KEYS)
    for k in got:
        if k not in known or (k in REASSEMBLED_KEYS and k not in want):
            out.append(f"{k}: not in intent (got {got[k]!r:.120})")
    return out


def where(row):
    return (f"channel {row['channel']} v{row['sequenceVersion']} "
            f"packet {row['sequenceNumber']} message {row['index']}")


LOOKAHEAD = 256   # intent rows kept ahead per channel to resync after a missing row


class _Matcher:
    """Matches intent rows with decode rows per channel, in order, holding
    only a short window of each channel in memory."""

    def __init__(self, strict, max_report):
        self.strict, self.max_report = strict, max_report
        self.want, self.got = {}, {}       # channel -> deque of (line number, row)
        self.compared, self.problems = 0, []

    def full(self):
        return len(self.problems) >= self.max_report

    def report(self, text):
        if not self.full():
            self.problems.append(text)

    def drain(self, final):
        for ch in sorted(set(self.want) | set(self.got)):
            want = self.want.setdefault(ch, deque())
            got = self.got.setdefault(ch, deque())
            while want and got and (final or len(want) > LOOKAHEAD) and not self.full():
                (wl, w), (gl, g) = want[0], got[0]
                if b3intent.row_key(w) != b3intent.row_key(g):
                    gk = b3intent.row_key(g)
                    if any(b3intent.row_key(r) == gk for _, r in list(want)[1:]):
                        self.report(f"{where(w)}: missing from decode (intent line {wl})")
                        want.popleft()
                    else:
                        self.report(f"{where(g)}: extra row in decode (decode line {gl})")
                        got.popleft()
                    continue
                self.compared += 1
                for d in diff_rows(w, g, self.strict):
                    self.report(f"{where(w)}: {d} (intent line {wl}, decode line {gl})")
                want.popleft()
                got.popleft()
            if final:
                for wl, w in want:
                    self.report(f"{where(w)}: missing from decode (intent line {wl})")
                for gl, g in got:
                    self.report(f"{where(g)}: extra row in decode (decode line {gl})")


def compare(expected, decoded, strict=False, max_report=10):
    """expected, decoded: iterables of (line number, row). Returns
    (rows compared, [mismatch text]), at most max_report of them."""
    m = _Matcher(strict, max_report)
    expected, decoded = iter(expected), iter(decoded)
    live_e = live_d = True
    while (live_e or live_d) and not m.full():
        if live_e:
            item = next(expected, None)
            if item is None:
                live_e = False
            else:
                m.want.setdefault(item[1]["channel"], deque()).append(item)
        if live_d:
            item = next(decoded, None)
            if item is None:
                live_d = False
            else:
                line_no, row = item
                absent = [k for k in ("channel", "sequenceVersion", "sequenceNumber", "index")
                          if k not in row]
                if absent:
                    return m.compared, [f"decode line {line_no}: no {', '.join(absent)}"]
                m.got.setdefault(row["channel"], deque()).append(item)
        m.drain(final=False)
    if not m.full():
        m.drain(final=True)
    return m.compared, m.problems


def order_difference(expected, decoded):
    """Feed A: the decode's rows in intent.jsonl's order, across channels."""
    for n, ((wl, w), (gl, g)) in enumerate(zip(expected, decoded)):
        if b3intent.row_key(w) != b3intent.row_key(g):
            return [f"row {n}: wire order differs: intent line {wl} is {where(w)}, "
                    f"decode line {gl} is {where(g)}"]
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("decode", help="decode.jsonl from the decoder")
    ap.add_argument("intent", help="intent.jsonl (the key)")
    ap.add_argument("--manifest", help="manifest.json (default: next to intent.jsonl)")
    ap.add_argument("--feed", default="A", choices=("A", "B"),
                    help="which feed's pcap was decoded")
    ap.add_argument("--strict", action="store_true",
                    help="also require news on every row of intent.jsonl that has it")
    ap.add_argument("--max-report", type=int, default=10)
    args = ap.parse_args(argv)

    manifest = b3intent.load_manifest(args.intent, args.manifest)
    expected = b3intent.feed_rows(args.intent, manifest, args.feed)
    compared, problems = compare(expected, b3intent.read_jsonl(args.decode),
                                 args.strict, args.max_report)
    if args.feed == "A" and not problems:
        problems = order_difference(b3intent.feed_rows(args.intent, manifest, "A"),
                                    b3intent.read_jsonl(args.decode))
    if problems:
        for p in problems:
            print(p)
        print(f"FAIL: first {len(problems)} mismatches shown")
        return 1
    print(f"OK: {compared} rows match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
