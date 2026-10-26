"""docs/DESIGN.md's hex examples must match what the tools produce."""

import re

import b3contract as c
import intent_to_events as ie
import rows
from conftest import REPO

DESIGN = REPO / "docs" / "DESIGN.md"


def design_example(name):
    """Bytes of the ```text block right after <!-- example:name -->."""
    text = DESIGN.read_text(encoding="utf-8")
    m = re.search(r"<!-- example:" + re.escape(name) + r" -->\s*```text\n(.*?)```", text, re.S)
    assert m, f"example {name} not found in DESIGN.md"
    hex_lines = [ln for ln in m.group(1).splitlines() if ln.strip() and not ln.startswith("#")]
    return bytes.fromhex(" ".join(hex_lines))


def records(row_list):
    return b"".join(e.pack() for e in ie.convert(row_list))


def test_events_header_example():
    assert design_example("events_header") == c.events_header().pack()


def test_sample_packet_example():
    assert design_example("sample_packet") == records(rows.sample_packet_rows())


def test_trade_event_example():
    assert design_example("trade_event") == records(rows.trade_event_rows())


def test_sample_packet_bytes_come_from_the_wire():
    """The record's securityID, price, size and time are the packet's own bytes
    (feed_generator/tests/fixtures/b3_sample_packet/packet.hex)."""
    hex_text = (rows.SAMPLE.parent / "packet.hex").read_text()
    packet = bytes.fromhex(" ".join(ln for ln in hex_text.splitlines()
                                    if not ln.startswith("#")))
    order_body = packet[16 + 12:16 + 84]
    rec = design_example("sample_packet")[:64]
    assert rec[16:24] == order_body[0:8]      # securityID
    assert rec[24:32] == order_body[12:20]    # mDEntryPx
    assert rec[32:40] == order_body[20:28]    # mDEntrySize
    assert rec[48:56] == order_body[56:64]    # transactTime
    assert rec[0:4] == packet[4:8]            # sequenceNumber
