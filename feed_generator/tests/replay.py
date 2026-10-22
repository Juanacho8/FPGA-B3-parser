"""Book replay with the client rules, shared by the scenario tests (no b3gen)."""

import json
from pathlib import Path

from book_builder import BookBuilder
import sbe_decoder
from wire import read_packets, top5


def replay(pcap_path, book_key_path, security_ids, unreliable=None):
    """Replay a feed with tests/book_builder.py (apply_packet), comparing
    every book key checkpoint when its as_of is reached.

    unreliable: {instrument: [channel, version, seq]}. From that packet on,
    the instrument's messages are not applied (its book is unreliable) and
    its checkpoints are not compared. Returns ({instrument: checkpoints
    compared}, number of key rows)."""
    unreliable = {name: tuple(key) for name, key in (unreliable or {}).items()}
    name_of = {sid: name for name, sid in security_ids.items()}
    rows = [json.loads(line) for line in Path(book_key_path).read_text().splitlines()]
    expect = {}
    for row in rows:
        for name, book in row["books"].items():
            expect.setdefault(tuple(book["as_of"]), []).append((name, book))
    builder = BookBuilder()
    checks = {name: 0 for name in security_ids}

    def trusted(name, key):
        start = unreliable.get(name)
        return start is None or key[0] != start[0] or key < start

    for p in read_packets(pcap_path):
        key = (p["channel"], p["version"], p["seq"])

        def skip(index, data, key=key):
            _, fields = sbe_decoder.decode(data)
            sid = (fields or {}).get("securityID")
            return sid in name_of and not trusted(name_of[sid], key)

        def after(index, key=key):
            for name, book in expect.pop(key + (index,), []):
                if trusted(name, key):
                    assert top5(builder, security_ids[name]) == (book["bids"], book["offers"]), \
                        (name, key)
                    checks[name] += 1

        builder.apply_packet(p["channel"], p["version"], p["seq"], p["messages"],
                             after=after, skip=skip)
    assert all(name not in checks or not trusted(name, key)
               for key, books in expect.items() for name, _ in books), "as_of never reached"
    return checks, len(rows)


CHANNEL_WIDE = {1, 10, 11}         # SequenceReset, SecurityGroupPhase, ChannelReset
BOOK_TEMPLATES = {9, 50, 51, 52}   # EmptyBook, Order_MBO, DeleteOrder_MBO, MassDeleteOrders_MBO


def check_unreliable(unreliable, lost_rows, channel_of):
    """Check a manifest's unreliable_books against what a client really
    lost: lost_rows are the intent rows of messages it never saw (missing
    packets, or the rest of a faulty one), in wire order.

    * Necessary: an instrument is marked from a packet where it lost a
      message, its own or a channel-wide one on its channel. Marking an
      instrument that lost nothing, or marking it earlier, fails.
    * Sufficient: an instrument that lost a book message is marked from that
      packet or an earlier one."""
    def packet(row):
        return (row["channel"], row["sequenceVersion"], row["sequenceNumber"])

    lost_at = {}
    for row in lost_rows:
        lost_at.setdefault(packet(row), []).append(row)
    for name, at in unreliable.items():
        rows = lost_at.get(tuple(at), [])
        assert any(r["instrument"] == name or
                   (r["instrument"] is None and r["templateId"] in CHANNEL_WIDE and
                    r["channel"] == channel_of[name]) for r in rows), \
            (name, at, "marked where it lost nothing")
    for row in lost_rows:
        if row["templateId"] in BOOK_TEMPLATES:
            at = unreliable.get(row["instrument"])
            assert at is not None and tuple(at) <= packet(row), (row["instrument"], packet(row), at)
