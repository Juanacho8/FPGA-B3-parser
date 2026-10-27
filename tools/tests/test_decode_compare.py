"""decode_compare: the schema-set feature check and the empty-data rule."""

import json

import decode_compare as dc


def write(tmp_path, rows):
    path = tmp_path / "decode.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def row(tid, bl, version, **extra):
    return {"templateId": tid, "blockLength": bl, "version": version, "fields": {}, **extra}


def test_schema_features_found(tmp_path):
    rows = [row(50, 56, 14, absent=["transactTime", "mDEntryPrevSize"]),
            row(50, 64, 15, absent=["mDEntryPrevSize"]), row(50, 80, 18),
            {**row(999, 20, 18), "fields": {"body": "00" * 20}}]
    problems, summary = dc.check_schema_rows(write(tmp_path, rows))
    assert problems == []
    assert "template 999 body of 20 bytes" in summary


def test_schema_features_missing(tmp_path):
    problems, _ = dc.check_schema_rows(write(tmp_path, [row(50, 72, 17)]))
    assert any("absent fields at versions 14 and 15" in p for p in problems)
    assert any("blockLength above the schema's" in p for p in problems)
    assert any("template 999" in p for p in problems)


def test_no_sets_fails(tmp_path, capsys):
    assert dc.main([str(tmp_path), "--cpp", "/bin/false"]) == 2
    assert "run make data" in capsys.readouterr().err
