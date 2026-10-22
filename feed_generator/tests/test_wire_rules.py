"""Event rules read from the wire alone (spec/LAYOUTS.md section 12.2).

Every scenario's pcap is decoded independently (tests/wire.py,
tests/sbe_decoder.py); nothing here asks b3gen what it meant.

* One time per event: every message of an event carries the same
  transactTime / mDEntryTimestamp (FEED_GENERATOR_CONTEXT.md section 5).
* RecoveryMsg (bit 5) appears only on Order_MBO, EmptyBook and ChannelReset.
"""

import pytest

from b3gen import main
import sbe_decoder
from wire import read_packets

EOE, RECOVERY = 0x80, 0x20
RECOVERY_TEMPLATES = {50, 9, 11}   # Order_MBO, EmptyBook, ChannelReset
EVENT_TIMES = ("transactTime", "mDEntryTimestamp")


def complete_feed(result):
    """A feed that lost nothing: Feed A, or Feed B when only A lost packets.
    None when packets are missing from both, or a packet is cut short."""
    missing = result.manifest["missing"]
    if result.manifest.get("faults") or missing["B"]:
        return None
    return result.run.pcap_path if not missing["A"] else result.feed_b_path


@pytest.mark.parametrize("name", main.SCENARIOS)
def test_one_time_per_event(runs, name):
    path = complete_feed(runs[name])
    if path is None:
        pytest.skip("events cut by missing or faulty packets")
    open_event = {}  # channel -> event times so far
    events = multi = 0
    for p in read_packets(path):
        if not p["seq"]:
            continue  # heartbeat
        for data in p["messages"]:
            header, f = sbe_decoder.decode(data)
            if f is None or header["templateId"] == 1:
                continue  # unknown template, SequenceReset: not in an event
            times = open_event.setdefault(p["channel"], [])
            times.append({f[k] for k in EVENT_TIMES if k in f})
            if f.get("matchEventIndicator", 0) & EOE:
                # At most one time (News has neither field: origTime only).
                assert len(set().union(*times)) <= 1, (p["channel"], p["seq"], times)
                events += 1
                multi += len(times) > 1
                del open_event[p["channel"]]
    assert not open_event, "event left open at the end of the file"
    assert events > 1000 and multi > 300


@pytest.mark.parametrize("name", main.SCENARIOS)
def test_recovery_bit_only_on_order_empty_book_and_channel_reset(runs, name):
    flagged = 0
    for path in (runs[name].run.pcap_path, runs[name].feed_b_path):
        for p in read_packets(path):
            for data in p["messages"]:
                header, f = sbe_decoder.decode(data)
                if f and f.get("matchEventIndicator", 0) & RECOVERY:
                    assert header["templateId"] in RECOVERY_TEMPLATES, (name, p["seq"], header)
                    flagged += 1
    if name in ("failover", "channel_reset", "instrument_reset", "weekly_start"):
        assert flagged  # the rule was exercised
