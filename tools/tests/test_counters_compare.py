"""counters_compare: the replay's stale rules, its guards, the unreliable_books check."""

import json

import pytest

import b3intent
import counters_compare as cc
from rows import order, row

TABLE = [(7, 68), (8, 68), (9, 70)]


def manifest(missing_a=(), faults=None):
    m = {"missing": {"A": [list(k) for k in missing_a], "B": []}, "unreliable_books": {}}
    if faults is not None:
        m["faults"] = faults
    return m


def run(rows, m, feed="A"):
    rp = cc.Replay(TABLE, m, feed)
    for pkt in cc.packets(rows):
        rp.packet(pkt)
    return rp


def o(sid, rpt, seq, ch=68, index=0, **kw):
    r = order(0, seq=seq, channel=ch, index=index, **kw)
    r["fields"].update(securityID=sid, rptSeq=rpt)
    return r


def test_gap_marks_the_channel_at_the_first_missing_number():
    rp = run([o(7, 1, 1), o(7, 2, 2), o(8, 1, 5), o(9, 1, 1, ch=70)], manifest([(68, 1, 3), (68, 1, 4)]))
    assert rp.seq["gaps"] == 2 and rp.seq["gap_events"] == 1
    assert rp.state[0]["first"] == [68, 1, 3] and rp.state[1]["first"] == [68, 1, 3]
    assert rp.state[2]["first"] is None


def test_gap_not_in_the_manifest_is_refused():
    with pytest.raises(cc.Broken, match="not in the manifest's missing list"):
        run([o(7, 1, 1), o(7, 2, 3)], manifest())


def test_rptseq_jump_marks_one_instrument_and_empty_book_clears():
    rows = [o(7, 1, 1), o(8, 1, 1, index=1), o(7, 3, 2),
            row(9, {"securityID": 7, "matchEventIndicator": 32, "mDEntryTimestamp": 1}, seq=3),
            o(7, 1, 4)]
    rp = run(rows, manifest())
    assert rp.state[0]["gaps"] == 1 and rp.state[0]["first"] == [68, 1, 2]
    assert rp.state[0]["stale"] is False                       # cleared by the EmptyBook
    assert rp.state[1]["first"] is None


def test_fault_marks_the_channel_at_the_packet_and_channel_reset_clears():
    faults = [{"fault": "bad_encoding", "channel": 68, "sequenceVersion": 1, "sequenceNumber": 2,
               "offset": 100, "messages_before": 1}]
    rows = [o(7, 1, 1), o(7, 2, 2), row(11, {"matchEventIndicator": 0xA0, "mDEntryTimestamp": 1}, seq=3)]
    rp = run(rows, manifest(faults=faults))
    assert rp.losses == 1
    assert rp.state[0]["first"] == [68, 1, 2] and rp.state[1]["first"] == [68, 1, 2]
    assert not rp.state[0]["stale"] and not rp.state[1]["stale"]


def test_reset_packet_and_heartbeats():
    rows = [o(7, 1, 40), row(1, {}, seq=41), row(2, {"nextSeqNo": 1}, seq=0),
            row(2, {"nextSeqNo": 5}, seq=0), o(7, 2, 1, version=2)]
    rp = run(rows, manifest())
    assert rp.seq["resets"] == 1 and rp.seq["reset_mismatches"] == 0
    assert rp.seq["heartbeats"] == 2 and rp.seq["heartbeat_mismatches"] == 1


def test_unknown_instrument_counted():
    rp = run([o(99, 1, 1)], manifest())
    assert rp.unknown == 1


def test_unreliable_books_check():
    got = {"instruments": {"7": {"first_stale": [68, 1, 900]}}}
    assert cc.check_unreliable({"WIN": [68, 1, 905]}, {"WIN": 7}, got, []) == []
    assert cc.check_unreliable({"WIN": [68, 1, 899]}, {"WIN": 7}, got, [])     # flagged too late
    assert cc.check_unreliable({"WIN": [70, 1, 905]}, {"WIN": 7}, got, [])     # wrong channel
    assert cc.check_unreliable({"DOL": [68, 1, 905]}, {"WIN": 7}, got, [])     # unknown name


def test_unreliable_books_shape_guard():
    with pytest.raises(b3intent.ManifestError, match="unreliable_books"):
        cc.check_unreliable_shape({"unreliable_books": {"WIN": [68, 1]}}, "m")
    with pytest.raises(b3intent.ManifestError, match="unreliable_books"):
        cc.check_unreliable_shape({}, "m")


def test_nothing_checked_fails(tmp_path, capsys):
    assert cc.main([str(tmp_path), "--cpp", "/bin/false"]) == 2
    assert "run make data" in capsys.readouterr().err
