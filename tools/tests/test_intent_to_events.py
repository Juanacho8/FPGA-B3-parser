"""intent_to_events: field mapping, the closing state machine, feed filtering."""

import json

import pytest

import b3contract as c
import b3intent
import intent_to_events as ie
from rows import heartbeat, order, row, stats, summary


def kinds(row_list):
    return [(e.kind, e.flags & 1) for e in ie.convert(row_list)]


# ---- 1. Field mapping (DESIGN.md 3.4) ----
def test_add_and_modify_fields():
    add, mod = ie.to_event(order(0, side="1")), ie.to_event(order(1, mei=0))
    assert (add.kind, add.side, add.aux, add.price, add.qty, add.order_id,
            add.exch_time, add.rpt_seq, add.flags) == (c.ADD, c.SIDE_OFFER, 9, 100, 5, 42,
                                                       1000, 3, c.FLAG_END_OF_EVENT)
    assert mod.kind == c.MODIFY and mod.aux == c.INT64_NULL and mod.flags == 0


def test_other_order_actions_are_not_on_port1():
    assert ie.to_event(order(2)) is None
    mass = row(52, {"securityID": 7, "matchEventIndicator": 0x80, "mDUpdateAction": 4,
                    "mDEntryType": "0", "transactTime": 1, "rptSeq": 1})
    assert ie.to_event(mass) is None


def test_absent_fields_become_their_null():
    v14 = order(0)
    for k in ("transactTime", "mDEntryPrevSize"):
        del v14["fields"][k]
    v14.update(version=14, absent=["transactTime", "mDEntryPrevSize"])
    assert ie.to_event(v14).exch_time == 0
    v15 = order(1)
    del v15["fields"]["mDEntryPrevSize"]
    v15["absent"] = ["mDEntryPrevSize"]
    assert ie.to_event(v15).aux & 0xFFFF_FFFF_FFFF_FFFF == 1 << 63
    delete = row(51, {"securityID": 7, "matchEventIndicator": 0, "mDEntryType": "0",
                      "mDEntrySize": 5, "secondaryOrderID": 1, "rptSeq": 1},
                 absent=["transactTime", "mDEntryPx"])
    e = ie.to_event(delete)
    assert e.price == c.INT64_NULL and e.exch_time == 0


def test_flags_recovery_and_implied():
    e = ie.to_event(order(0, mei=0x80 | 0x20 | 0x10))
    assert e.flags == c.FLAG_END_OF_EVENT | c.FLAG_RECOVERY | c.FLAG_IMPLIED


def test_session_word():
    phase = row(10, {"securityGroup": "IND", "matchEventIndicator": 128, "tradingSessionID": 1,
                     "tradingSessionSubID": 17, "securityTradingEvent": 4, "tradeDate": 20745,
                     "tradSesOpenTime": 0, "transactTime": 77})
    e = ie.to_event(phase)
    assert e.kind == c.SESSION and e.exch_time == 77 and e.instrument_id == 0
    assert e.aux.to_bytes(8, "little") == bytes([1, 17, 4]) + (20745).to_bytes(2, "little") + b"IND"


def test_trade_bust_seq_reset_clears():
    bust = row(57, {"securityID": 7, "matchEventIndicator": 128, "tradingSessionID": 1,
                    "mDEntryPx": 10, "mDEntrySize": 2, "tradeID": 147, "tradeDate": 1,
                    "transactTime": 5, "rptSeq": 9})
    e = ie.to_event(bust)
    assert (e.kind, e.order_id, e.price, e.qty, e.aux) == (c.TRADE_BUST, 147, 10, 2, 0)
    assert ie.to_event(row(1, {})) == c.Event(1, 1, 68, 0, c.SEQ_RESET, venue_msg_type=1)
    reset = ie.to_event(row(11, {"matchEventIndicator": 0xA0, "mDEntryTimestamp": 3}))
    assert (reset.kind, reset.flags, reset.exch_time) == (
        c.CLEAR_CHANNEL, c.FLAG_END_OF_EVENT | c.FLAG_RECOVERY, 3)
    empty = ie.to_event(row(9, {"securityID": 7, "matchEventIndicator": 0x20,
                                "mDEntryTimestamp": 4}))
    assert (empty.kind, empty.instrument_id, empty.exch_time) == (c.CLEAR_BOOK, 7, 4)


def test_not_on_port1():
    for t in (2, 5, 15, 17, 21, 22, 24, 25, 28, 29, 56, 999):
        assert ie.to_event(row(t, {"matchEventIndicator": 128})) is None


# ---- 2. Closing state machine (DESIGN.md 3.5) ----
def test_standalone_statistic_makes_no_marker():
    assert kinds([stats()]) == []


def test_marker_closes_open_event():
    out = list(ie.convert([summary(), stats(index=1)]))
    assert [e.kind for e in out] == [c.EXEC_SUMMARY, c.END_OF_EVENT]
    m = out[1]
    assert (m.msg_index, m.venue_msg_type, m.exch_time, m.flags, m.instrument_id) == \
        (1, 56, 2000, c.FLAG_END_OF_EVENT, 0)


def test_port1_message_with_eoe_takes_the_flag():
    assert kinds([order(0, mei=0x80)]) == [(c.ADD, 1)]
    assert kinds([order(0, mei=0), order(1, mei=0x80, index=1)]) == [(c.ADD, 0), (c.MODIFY, 1)]


def test_eoe_closes_once():
    # After the close the channel is idle again: a second statistic is standalone.
    out = kinds([order(0, mei=0), stats(index=1), stats(index=2)])
    assert out == [(c.ADD, 0), (c.END_OF_EVENT, 1)]


def test_channels_are_independent():
    out = kinds([order(0, mei=0, channel=68), stats(channel=70), stats(channel=68, index=1)])
    assert out == [(c.ADD, 0), (c.END_OF_EVENT, 1)]


def test_messages_without_mei_do_not_change_state():
    unknown = row(999, {"body": "00"})
    assert kinds([heartbeat(), unknown, stats()]) == []
    assert kinds([order(0, mei=0), heartbeat(), row(1, {}), unknown, stats(index=1)]) == \
        [(c.ADD, 0), (c.SEQ_RESET, 0), (c.END_OF_EVENT, 1)]


def test_non_port1_message_without_eoe_opens_an_event():
    # A HighPrice with mEI 0 opens; the ExecutionStatistics then needs a marker.
    high = row(24, {"securityID": 7, "matchEventIndicator": 0, "mDUpdateAction": 0,
                    "tradeDate": 1, "mDEntryPx": 1, "mDEntryTimestamp": 1, "rptSeq": 1})
    assert kinds([high, stats(index=1)]) == [(c.END_OF_EVENT, 1)]


def test_unsupported_order_action_with_eoe_gets_a_marker():
    assert kinds([summary(), order(2, mei=0x80, index=1)]) == [(c.EXEC_SUMMARY, 0),
                                                               (c.END_OF_EVENT, 1)]


# ---- 3. Feed filtering (b3intent) ----
def write_set(tmp_path, row_list, manifest):
    (tmp_path / "intent.jsonl").write_text("".join(json.dumps(r) + "\n" for r in row_list))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path / "intent.jsonl"


def test_missing_packets_are_left_out(tmp_path):
    data = [order(0, seq=1), order(0, seq=2), order(0, seq=3)]
    path = write_set(tmp_path, data, {"missing": {"A": [[68, 1, 2]], "B": []}})
    seen = [r["sequenceNumber"] for _, r in b3intent.feed_rows(path, b3intent.load_manifest(path), "A")]
    assert seen == [1, 3]
    seen_b = [r["sequenceNumber"] for _, r in b3intent.feed_rows(path, b3intent.load_manifest(path), "B")]
    assert seen_b == [1, 2, 3]


def test_malformed_tail_is_discarded(tmp_path):
    # The packet holding the EndOfEvent loses it: the event stays open, and the
    # next channel EndOfEvent (a standalone statistic) closes it with a marker.
    data = [order(0, mei=0, seq=5, index=0), order(1, mei=0x80, seq=5, index=1),
            stats(seq=6, index=0)]
    manifest = {"missing": {"A": [], "B": []},
                "faults": [{"fault": "short", "channel": 68, "after": [68, 1, 5], "length": 10},
                           {"fault": "bad_encoding", "channel": 68, "sequenceVersion": 1,
                            "sequenceNumber": 5, "offset": 100, "messages_before": 1}]}
    path = write_set(tmp_path, data, manifest)
    kept = [r for _, r in b3intent.feed_rows(path, b3intent.load_manifest(path), "A")]
    assert [(r["sequenceNumber"], r["index"]) for r in kept] == [(5, 0), (6, 0)]
    assert kinds(kept) == [(c.ADD, 0), (c.END_OF_EVENT, 1)]


def test_heartbeats_are_never_dropped(tmp_path):
    path = write_set(tmp_path, [heartbeat()], {"missing": {"A": [[68, 1, 0]], "B": []}})
    assert len(list(b3intent.feed_rows(path, b3intent.load_manifest(path), "A"))) == 1


# ---- 4. Command line ----
def test_cli_writes_header_and_records(tmp_path):
    path = write_set(tmp_path, [summary(), stats(index=1)], {"missing": {"A": [], "B": []}})
    out = tmp_path / "events.bin"
    assert ie.main([str(path), "-o", str(out), "--tracked-depth", "32"]) == 0
    header, events = c.read_events(out.read_bytes())
    assert header.tracked_depth == 32 and header.published_depth == 5
    assert [e.kind for e in events] == [c.EXEC_SUMMARY, c.END_OF_EVENT]


def test_cli_refuses_feed_b(tmp_path, capsys):
    path = write_set(tmp_path, [summary()], {"missing": {"A": [], "B": []}})
    assert ie.main([str(path), "-o", str(tmp_path / "e.bin"), "--feed", "B"]) == 2
    assert "Feed B" in capsys.readouterr().err


@pytest.mark.parametrize("feed", ["A"])
def test_every_generated_set(needs_data, tmp_path, feed):
    """Runs the converter on every set in B3_DATA and checks the output is a
    well-formed events.bin with one close per marker-or-flag."""
    from conftest import generated_sets
    sets = generated_sets()
    assert sets, "B3_DATA has no intent.jsonl"
    for name, folder in sets:
        out = tmp_path / (name.replace("/", "_") + ".bin")
        assert ie.main([str(folder / "intent.jsonl"), "-o", str(out), "--feed", feed]) == 0
        header, events = c.read_events(out.read_bytes())
        assert events, name
        assert all(1 <= e.kind <= 12 for e in events), name
