"""b3intent: reads the feed generator's answer keys.

What it does:
  Streams intent.jsonl and turns it into the rows a correct decoder of one
  feed's pcap sees, using manifest.json:
    * intent.jsonl lists every message published, in Feed A wire order,
      including packets a feed later lost.
    * manifest "missing"[feed] names the packets that feed does not carry.
    * manifest "faults" (malformed set) names the damaged packets and how
      many complete messages come before each fault. Our malformed policy
      (docs/DESIGN.md section 6) keeps those and discards the rest of the
      packet. The stray short datagram has no intent row, so nothing to drop.

What it must not do:
  Import b3gen, or read the pcap. The answer keys are the only input.
"""

import json
from pathlib import Path


class FeedOrderUnknown(ValueError):
    """The intent log cannot give this feed's record order."""


def load_manifest(intent_path, manifest_path=None):
    path = Path(manifest_path) if manifest_path else Path(intent_path).with_name("manifest.json")
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if line.strip():
                yield line_no, json.loads(line)


def packet_key(row):
    return (row["channel"], row["sequenceVersion"], row["sequenceNumber"])


def row_key(row):
    return (row["channel"], row["sequenceVersion"], row["sequenceNumber"], row["index"])


def feed_filter(manifest, feed):
    """keep(row) -> bool for the rows a decoder of `feed` ("A" or "B") sees."""
    if feed not in ("A", "B"):
        raise ValueError(f"feed must be A or B, got {feed!r}")
    missing = {tuple(k) for k in manifest.get("missing", {}).get(feed, [])}
    cut = {}  # packet -> messages kept before the fault
    for fault in manifest.get("faults") or []:
        if "sequenceNumber" in fault:
            cut[(fault["channel"], fault["sequenceVersion"], fault["sequenceNumber"])] = \
                fault["messages_before"]

    def keep(row):
        key = packet_key(row)
        if row["sequenceNumber"] != 0 and key in missing:
            return False
        limit = cut.get(key)
        return limit is None or row["index"] < limit
    return keep


def feed_rows(intent_path, manifest, feed):
    """(line number, row) for each intent row a decoder of `feed` sees, in
    Feed A wire order. Per channel that is also Feed B's order."""
    keep = feed_filter(manifest, feed)
    for line_no, row in read_jsonl(intent_path):
        if keep(row):
            yield line_no, row
