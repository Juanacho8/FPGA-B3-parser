"""compare_decode: intent against itself passes, altered copies fail."""

import copy
import json

import pytest

import compare_decode as cd
from rows import heartbeat, order, stats, summary, trade_event_rows


def base_rows():
    rows = trade_event_rows()
    older = order(0, seq=17, channel=70, index=0)
    del older["fields"]["transactTime"], older["fields"]["mDEntryPrevSize"]
    older.update(version=14, absent=["transactTime", "mDEntryPrevSize"])
    nulled = order(0, seq=18, channel=70, index=0)
    nulled["null"] = ["mDEntryPrevSize"]
    return rows + [older, nulled, heartbeat(channel=70), heartbeat(channel=70),
                   summary(channel=68, seq=4), stats(channel=68, seq=4, index=1)]


def write(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def run(tmp_path, decoded, expected=None, manifest=None, extra=()):
    expected = base_rows() if expected is None else expected
    intent = write(tmp_path, "intent.jsonl", expected)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest or {"missing": {"A": [], "B": []}}))
    decode = write(tmp_path, "decode.jsonl", decoded)
    return cd.main([str(decode), str(intent), *extra])


def test_intent_against_itself_passes(tmp_path, capsys):
    assert run(tmp_path, base_rows()) == 0
    assert "OK" in capsys.readouterr().out


def test_changed_field_fails(tmp_path, capsys):
    rows = base_rows()
    rows[2]["fields"]["mDEntrySize"] += 1
    assert run(tmp_path, rows) == 1
    out = capsys.readouterr().out
    assert "channel 70 v1 packet 16 message 2: fields.mDEntrySize: want 200, got 201" in out


def test_changed_type_fails(tmp_path):
    rows = base_rows()
    rows[2]["fields"]["mDEntrySize"] = "200"
    assert run(tmp_path, rows) == 1


def test_missing_row_fails(tmp_path, capsys):
    rows = base_rows()
    del rows[1]
    assert run(tmp_path, rows) == 1
    assert "packet 16 message 1: missing from decode" in capsys.readouterr().out


def test_extra_row_fails(tmp_path, capsys):
    rows = base_rows()
    rows.insert(2, copy.deepcopy(rows[1]) | {"index": 9})
    assert run(tmp_path, rows) == 1
    assert "message 9: extra row in decode" in capsys.readouterr().out


def test_null_swapped_for_absent_fails(tmp_path, capsys):
    rows = base_rows()
    nulled = rows[5]
    assert nulled["null"] == ["mDEntryPrevSize"]
    del nulled["fields"]["mDEntryPrevSize"]
    nulled["null"] = []
    nulled["absent"] = ["mDEntryPrevSize"]
    assert run(tmp_path, rows) == 1
    out = capsys.readouterr().out
    assert "null: missing ['mDEntryPrevSize']" in out
    assert "absent: extra ['mDEntryPrevSize']" in out


def test_absent_swapped_for_null_fails(tmp_path):
    rows = base_rows()
    older = rows[4]
    older.pop("absent")
    older["fields"].update(transactTime=0, mDEntryPrevSize=-(1 << 63))
    older["null"] = ["transactTime", "mDEntryPrevSize"]
    assert run(tmp_path, rows) == 1


def test_header_value_fails(tmp_path):
    rows = base_rows()
    rows[0]["blockLength"] = 72
    assert run(tmp_path, rows) == 1


def test_channel_interleaving_does_not_matter(tmp_path):
    """Feed B merges channels in another order; per channel order is what counts."""
    rows = base_rows()
    reordered = [r for r in rows if r["channel"] == 68] + [r for r in rows if r["channel"] == 70]
    assert run(tmp_path, reordered, extra=["--feed", "B"]) == 0
    # Feed A follows intent.jsonl's wire order across channels too.
    assert rows != reordered
    assert run(tmp_path, reordered) == 1


def test_instrument_is_never_required(tmp_path):
    rows = base_rows()
    stripped = copy.deepcopy(rows)
    for r in stripped:
        r.pop("instrument", None)
    assert run(tmp_path, stripped) == 0
    assert run(tmp_path, stripped, extra=["--strict"]) == 0
    wrong = copy.deepcopy(rows)
    wrong[0]["instrument"] = "VALE3"
    assert run(tmp_path, wrong) == 1      # compared when present


def test_news_is_required_by_strict(tmp_path):
    rows = base_rows()
    rows[-1]["news"] = {"headline": "h", "text": "t"}
    without = copy.deepcopy(rows)
    del without[-1]["news"]
    assert run(tmp_path, rows, rows, extra=["--strict"]) == 0
    assert run(tmp_path, without, rows) == 0
    assert run(tmp_path, without, rows, extra=["--strict"]) == 1
    wrong = copy.deepcopy(rows)
    wrong[-1]["news"]["text"] = "x"
    assert run(tmp_path, wrong, rows) == 1


def test_keys_intent_does_not_have_fail(tmp_path, capsys):
    rows = base_rows()
    extra = copy.deepcopy(rows)
    extra[1]["body_fault"] = {"reason": "var_data_past_end", "at": "text"}
    assert run(tmp_path, extra) == 1
    assert "body_fault: not in intent" in capsys.readouterr().out
    no_null = copy.deepcopy(rows)
    del no_null[0]["null"]
    assert run(tmp_path, no_null) == 1


def test_feed_filter_applies(tmp_path):
    """A decode of a feed that lost a packet must not contain it."""
    rows = base_rows()
    manifest = {"missing": {"A": [[70, 1, 16]], "B": []}}
    without = [r for r in rows if not (r["channel"] == 70 and r["sequenceNumber"] == 16)]
    assert run(tmp_path, without, rows, manifest) == 0
    assert run(tmp_path, rows, rows, manifest) == 1
    assert run(tmp_path, rows, rows, manifest, extra=["--feed", "B"]) == 0


def test_every_generated_set_against_itself(needs_data):
    from conftest import generated_sets
    import b3intent
    for name, folder in generated_sets():
        if name == "large":
            continue  # 1M rows: covered by hand, ~15 s
        intent = folder / "intent.jsonl"
        manifest = b3intent.load_manifest(intent)
        for feed in ("A", "B"):
            compared, problems = cd.compare(b3intent.feed_rows(intent, manifest, feed),
                                            b3intent.feed_rows(intent, manifest, feed))
            assert not problems, (name, feed, problems)
            assert compared > 0
