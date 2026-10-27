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
    * manifest "reordered"[feed] (reorder scenario) names the packets that
      arrive late on that feed. A correct decoder has a reorder window
      (docs/CPP_BASELINE_CONTEXT.md section 6): it holds the packets after
      the hole and releases them, in sequence order, when the late packet
      arrives. Feed A's rows come out in that release order.
  EXPECTED_FAILURES names the sets whose key describes a decoder feature
  not built yet; the compare tools report them as XFAIL, and as a failure
  (XPASS) once they match, so the entry gets removed.

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

# manifest "scenario" -> why the C++ cannot match its key yet.
EXPECTED_FAILURES = {"reorder": "reorder window not built yet"}


def expected_failure(manifest):
    """The reason this set is expected to fail, or None."""
    return EXPECTED_FAILURES.get(manifest.get("scenario"))


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_key(k):
    return isinstance(k, list) and len(k) == 3 and all(map(_is_int, k))


def validate_manifest(manifest, where="manifest.json"):
    """Raises ManifestError unless the keys these tools read have the shape
    they expect: "missing" = {"A": [[ch, ver, seq], ...], "B": [...]},
    "reordered" (optional: absent in manifests from before the reorder
    scenario, meaning no late packets) = {"A": [{"packet": [ch, ver, seq],
    "after": [ch, ver, seq], "depth": n}, ...], "B": [...]}, where after is
    the packet depth numbers later on the same channel and version, and
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
        if not isinstance(keys, list) or not all(map(_is_key, keys)):
            raise ManifestError(f"{where}: 'missing.{feed}' must be a list of "
                                f"[channel, sequenceVersion, sequenceNumber]")
    reordered = manifest.get("reordered")
    if reordered is not None:
        if not isinstance(reordered, dict) or set(reordered) != {"A", "B"}:
            raise ManifestError(f"{where}: 'reordered' must be an object with keys 'A' and 'B', "
                                f"got {reordered!r:.80}")
        for feed in ("A", "B"):
            if not isinstance(reordered[feed], list):
                raise ManifestError(f"{where}: 'reordered.{feed}' must be a list")
            for i, r in enumerate(reordered[feed]):
                ok = isinstance(r, dict) and set(r) == {"packet", "after", "depth"} and \
                    _is_key(r["packet"]) and _is_key(r["after"]) and _is_int(r["depth"]) and \
                    r["depth"] >= 1 and r["after"] == r["packet"][:2] + [r["packet"][2] + r["depth"]]
                if not ok:
                    raise ManifestError(f"{where}: reordered.{feed}[{i}] must be {{packet, after, "
                                        f"depth}} with after depth numbers later on its channel, "
                                        f"got {r!r:.80}")
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


def reordered(manifest, feed):
    """{late packet: packet it arrives right after} on `feed`."""
    return {tuple(r["packet"]): tuple(r["after"])
            for r in (manifest.get("reordered") or {}).get(feed, [])}


def feed_rows(intent_path, manifest, feed):
    """(line number, row) for each intent row a decoder of `feed` sees, in
    the order it releases them: per channel, sequence order (both feeds).
    Across channels, Feed A's order: its wire order, except that from a
    late packet up to the packet it arrives after, that channel's packets
    are held and released together, in sequence order, right before the
    packet it arrives after (CPP_BASELINE_CONTEXT.md section 6). Feed B's
    order across channels is not in the intent log (DESIGN.md 3.7)."""
    keep = feed_filter(manifest, feed)
    late = reordered(manifest, feed)
    held = {}  # channel -> (packet that releases them, [(line number, row)])
    for line_no, row in read_jsonl(intent_path):
        if not keep(row):
            continue
        key, ch = packet_key(row), row["channel"]
        if ch in held and row["sequenceNumber"] != 0:
            until, rows = held[ch]
            if key != until:
                rows.append((line_no, row))
                continue
            del held[ch]
            yield from rows
        elif key in late and row["index"] == 0:
            held[ch] = (late[key], [(line_no, row)])
            continue
        yield line_no, row
    if held:
        raise ManifestError(f"reordered.{feed}: packets {sorted(held)} wait for a packet that "
                            f"never comes")
