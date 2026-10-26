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


class ManifestError(ValueError):
    """manifest.json does not have the shape these tools read."""


# Fault kinds the feed generator's malformed set writes (b3gen/scenarios.py
# FAULTS). feed_filter's rule (keep messages before messages_before) is right
# for every one of them; a new kind may need another rule, so it is refused.
KNOWN_FAULT_KINDS = ("short", "length_past_end", "length_zero", "length_too_small",
                     "bad_encoding", "truncated")
PACKET_FIELDS = ("channel", "sequenceVersion", "sequenceNumber")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate_manifest(manifest, where="manifest.json"):
    """Raises ManifestError unless the keys these tools read have the shape
    they expect: "missing" = {"A": [[ch, ver, seq], ...], "B": [...]}, and
    "faults" (optional) = [{"fault": known kind, ...}], where every kind but
    "short" names its packet and messages_before."""
    if not isinstance(manifest, dict):
        raise ManifestError(f"{where}: not a JSON object")
    missing = manifest.get("missing")
    if not isinstance(missing, dict) or not {"A", "B"} <= set(missing):
        raise ManifestError(f"{where}: 'missing' must be an object with keys 'A' and 'B', "
                            f"got {missing!r:.80}")
    for feed in ("A", "B"):
        keys = missing[feed]
        if not isinstance(keys, list) or not all(
                isinstance(k, list) and len(k) == 3 and all(map(_is_int, k)) for k in keys):
            raise ManifestError(f"{where}: 'missing.{feed}' must be a list of "
                                f"[channel, sequenceVersion, sequenceNumber]")
    faults = manifest.get("faults")
    if faults is None:
        return manifest
    if not isinstance(faults, list):
        raise ManifestError(f"{where}: 'faults' must be a list, got {type(faults).__name__}")
    for i, fault in enumerate(faults):
        if not isinstance(fault, dict) or not isinstance(fault.get("fault"), str):
            raise ManifestError(f"{where}: faults[{i}] has no 'fault' kind: {fault!r:.80}")
        kind = fault["fault"]
        if kind not in KNOWN_FAULT_KINDS:
            raise ManifestError(f"{where}: faults[{i}] has unknown fault kind {kind!r} "
                                f"(known: {', '.join(KNOWN_FAULT_KINDS)})")
        if kind == "short":
            continue
        for field in PACKET_FIELDS + ("messages_before",):
            if not _is_int(fault.get(field)):
                raise ManifestError(f"{where}: faults[{i}] ({kind}) needs an integer {field!r}")
    return manifest


def load_manifest(intent_path, manifest_path=None):
    """manifest.json, checked by validate_manifest."""
    path = Path(manifest_path) if manifest_path else Path(intent_path).with_name("manifest.json")
    return validate_manifest(json.loads(path.read_text(encoding="utf-8")), str(path))


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
    validate_manifest(manifest)
    missing = {tuple(k) for k in manifest["missing"][feed]}
    cut = {}  # packet -> messages kept before the fault
    for fault in manifest.get("faults") or []:
        if fault["fault"] != "short":
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
