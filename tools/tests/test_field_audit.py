"""The field audit (DESIGN.md 3.10): every field the C++ reads has a row with a rule."""

import field_audit as fa


def test_every_read_has_a_row_and_every_row_a_rule():
    assert fa.problems() == []


def test_generated_files_are_up_to_date():
    assert fa.main(["--check"]) == 0


def test_a_read_without_a_row_fails(tmp_path):
    src = tmp_path / "sink.hpp"
    src.write_text("if constexpr (Id == L::Order_MBO_50::template_id) {\n"
                   "    r.x = value_or_null<T::mDInsertTimestamp>(m);\n}\n")
    assert any("Order_MBO_50.mDInsertTimestamp" in p for p in fa.problems(sources=[src]))


def test_a_row_without_a_rule_fails():
    rules = dict(fa.RULES)
    rules[("Order_MBO_50", "mDEntryPx")] = fa.Rule("  ")
    assert any("Order_MBO_50.mDEntryPx has no rule" in p for p in fa.problems(rules=rules))
    rules[("Order_MBO_50", "noSuchField")] = fa.Rule("x")
    assert any("noSuchField" in p for p in fa.problems(rules=rules))


def test_counts_from_intent_rows():
    base = {"templateId": 50, "absent": [], "null": [],
            "fields": {"mDUpdateAction": 1, "mDEntryType": "0", "mDEntryPrevSize": 5}}
    rows = [dict(base, null=["mDEntryPrevSize"]),                              # CHANGE, prev null
            dict(base, fields=dict(base["fields"], mDUpdateAction=0), null=["mDEntryPrevSize"]),   # NEW: not read
            dict(base, fields=dict(base["fields"], mDEntryType="X")),            # invalid side
            {"templateId": 51, "absent": ["mDEntryPx", "transactTime"], "null": [],
             "fields": {"mDEntryType": "1"}}]
    c = fa.counts(rows)
    assert c["Order_MBO_50.mDEntryPrevSize"] == [0, 1, 0]
    assert c["Order_MBO_50.mDEntryType"] == [0, 0, 1]
    assert c["DeleteOrder_MBO_51.mDEntryPx"] == [1, 0, 0]
    assert c["DeleteOrder_MBO_51.transactTime"] == [1, 0, 0]
