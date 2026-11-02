#!/usr/bin/env python3
"""field_audit: every field the event sink, tracker and books read, with our rule.

What it does:
  Declares, per (template, field) the C++ reads on the way to port 1 and
  port 2 (cpp_baseline/include/b3/event_sink.hpp and tracker.hpp; the
  books read only port 1 records), our rule when the field is absent (a
  block shorter than the field, an older schema version) or null, and,
  for a side field, when its value is invalid. Joins it with the parsed
  schema (tools/gen_layouts.py: presence, null value, sinceVersion) and
  writes:
    * the audit table in docs/DESIGN.md 3.10, between its markers
    * cpp_baseline/include/b3/field_audit.hpp: the rows, and per template a
      function counting, per row, the messages where the field is absent,
      null or invalid (the tracker calls it on every dispatched message;
      the counts are in the counters JSON, "fields")
  The Python side counts the same from intent.jsonl (counts()), for
  tools/counters_compare.py. Fields read for every template that has them
  (securityID and rptSeq by the tracker, matchEventIndicator and the time
  field by the event sink) are declared once, for all templates.
  tools/tests/test_field_audit.py fails if the C++ reads a field without a
  row, if a row has no rule, or if the generated files are out of date.

What it must not do:
  Import b3gen. Change a rule without the C++ that applies it.

Usage:
  python3 tools/field_audit.py           write both files (make field-audit)
  python3 tools/field_audit.py --check   exit 1 if either is out of date
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import gen_layouts   # noqa: E402

DESIGN = REPO / "docs" / "DESIGN.md"
HEADER = REPO / "cpp_baseline" / "include" / "b3" / "field_audit.hpp"
SOURCES = [REPO / "cpp_baseline" / "include" / "b3" / n for n in ("event_sink.hpp", "tracker.hpp")]
BEGIN, END = "<!-- field-audit:begin -->", "<!-- field-audit:end -->"

# Templates on port 1 (DESIGN.md 3.2) and the mDUpdateAction values that put them there.
PORT1 = {"Order_MBO_50": ("NEW", "CHANGE"), "MassDeleteOrders_MBO_52": ("DELETE_THRU",)}
ACTION = {"NEW": 0, "CHANGE": 1, "DELETE": 2, "DELETE_THRU": 3}
TIME_FIELDS = ("transactTime", "mDEntryTimestamp", "origTime")   # decode.hpp TimeField
UNAPPLIABLE = "**unappliable** (3.6.4): no level changes, the instrument goes STALE, cause 7"


@dataclass(frozen=True)
class Rule:
    text: str
    when: tuple = ()          # mDUpdateAction values the row applies to; () = every message
    side: bool = False        # also count values other than '0' and '1' as invalid
    reason: str = ""          # the unappliable reason it raises, if any


# (template, field) -> Rule. Template "*": every template that has the field.
RULES = {
    # Order_MBO (50): NEW -> ADD, CHANGE -> MODIFY
    ("Order_MBO_50", "mDUpdateAction"): Rule(
        "Read on every message. Absent reads 0 (NEW). Values other than NEW and CHANGE: not on port 1 (3.2)"),
    ("Order_MBO_50", "mDEntryType"): Rule(
        "Absent reads 0. A value other than '0' (bid) or '1' (offer) gives side 0: " + UNAPPLIABLE
        + " (`no_side`)", when=("NEW", "CHANGE"), side=True, reason="no_side"),
    ("Order_MBO_50", "mDEntryPx"): Rule(
        "Null: a market-on-auction order, price INT64_MIN on port 1; the price-level books count it "
        "(`null_price_orders`) and keep it out of the levels, the MBO book keeps the order",
        when=("NEW", "CHANGE")),
    ("Order_MBO_50", "mDEntrySize"): Rule(
        "Absent reads 0. Every value is applied (two's-complement arithmetic); a level's qty going negative "
        "is a consistency error", when=("NEW", "CHANGE")),
    ("Order_MBO_50", "secondaryOrderID"): Rule(
        "Absent reads 0. Not used by the price-level books; the MBO book keys the order by it",
        when=("NEW", "CHANGE")),
    ("Order_MBO_50", "enteringFirm"): Rule("Null or absent: aux 0", when=("NEW",)),
    ("Order_MBO_50", "mDEntryPrevSize"): Rule(
        "Null, or absent before v16: aux INT64_MIN, " + UNAPPLIABLE + " (`change_no_prev_size`). "
        "Read on CHANGE only: a NEW has none", when=("CHANGE",), reason="change_no_prev_size"),
    # DeleteOrder_MBO (51): DELETE
    ("DeleteOrder_MBO_51", "mDEntryType"): Rule(
        "Absent reads 0. A value other than '0' or '1' gives side 0: " + UNAPPLIABLE + " (`no_side`)",
        side=True, reason="no_side"),
    ("DeleteOrder_MBO_51", "mDEntryPx"): Rule(
        "Null, or absent before v15: price INT64_MIN, " + UNAPPLIABLE + " (`delete_no_price`); the MBO book "
        "removes the order by id", reason="delete_no_price"),
    ("DeleteOrder_MBO_51", "mDEntrySize"): Rule(
        "Absent reads 0. Every value is applied; a qty going negative, or a level at count 0 with qty left, "
        "is a consistency error"),
    ("DeleteOrder_MBO_51", "secondaryOrderID"): Rule(
        "Absent reads 0. Not used by the price-level books; the MBO book finds the order by it"),
    # MassDeleteOrders_MBO (52): DELETE_THRU -> CLEAR_SIDE
    ("MassDeleteOrders_MBO_52", "mDUpdateAction"): Rule(
        "Read on every message. Absent reads 0. Values other than DELETE_THRU: not on port 1 (3.2)"),
    ("MassDeleteOrders_MBO_52", "mDEntryType"): Rule(
        "Absent reads 0. A value other than '0' or '1' gives side 0: " + UNAPPLIABLE + " (`no_side`)",
        when=("DELETE_THRU",), side=True, reason="no_side"),
    # ChannelReset, EmptyBook: only the generic fields below
    # ExecutionSummary (55)
    ("ExecutionSummary_55", "lastPx"): Rule("Absent reads 0. Copied to port 1; no book uses it"),
    ("ExecutionSummary_55", "fillQty"): Rule("Absent reads 0. Copied to port 1; no book uses it"),
    ("ExecutionSummary_55", "aggressorSide"): Rule("Absent reads 0. Copied to aux; no book uses it"),
    # Trade (53), TradeBust (57)
    ("Trade_53", "mDEntryPx"): Rule("Absent reads 0. Copied to port 1; trades never touch a book"),
    ("Trade_53", "mDEntrySize"): Rule("Absent reads 0. Copied to port 1; trades never touch a book"),
    ("Trade_53", "tradeID"): Rule("Absent reads 0. Copied to order_id"),
    ("Trade_53", "mDEntryBuyer"): Rule("Null or absent: 0 in aux's high word"),
    ("Trade_53", "mDEntrySeller"): Rule("Null or absent: 0 in aux's low word"),
    ("TradeBust_57", "mDEntryPx"): Rule("Absent reads 0. Copied to port 1; no book uses it"),
    ("TradeBust_57", "mDEntrySize"): Rule("Absent reads 0. Copied to port 1; no book uses it"),
    ("TradeBust_57", "tradeID"): Rule("Absent reads 0. Copied to order_id"),
    # SecurityGroupPhase (10), SecurityStatus (3): the session word
    ("SecurityGroupPhase_10", "securityGroup"): Rule("Absent: 0 bytes in the session word"),
    ("SecurityGroupPhase_10", "tradingSessionID"): Rule("Absent reads 0 in the session word"),
    ("SecurityGroupPhase_10", "tradingSessionSubID"): Rule("Absent reads 0 in the session word"),
    ("SecurityGroupPhase_10", "securityTradingEvent"): Rule("Null (255) kept in the session word; absent: 255"),
    ("SecurityGroupPhase_10", "tradeDate"): Rule("Absent reads 0 in the session word"),
    ("SecurityStatus_3", "tradingSessionID"): Rule("Absent reads 0 in the session word"),
    ("SecurityStatus_3", "securityTradingStatus"): Rule("Absent reads 0 in the session word"),
    ("SecurityStatus_3", "securityTradingEvent"): Rule("Null (255) kept in the session word; absent: 255"),
    ("SecurityStatus_3", "tradeDate"): Rule("Absent reads 0 in the session word"),
    # Sequence (2), a heartbeat: the tracker passes nextSeqNo to the sequencer
    ("Sequence_2", "nextSeqNo"): Rule(
        "Absent reads 0: checked against the next expected number like any value (`heartbeat_mismatches`)"),
    # Every template that has the field
    ("*", "securityID"): Rule(
        "Required, never null. Absent: no rptSeq or stale tracking, not booked; on port 1 instrument_id 0"),
    ("*", "rptSeq"): Rule("Null (0) or absent: no rptSeq check; port 1 rpt_seq 0"),
    ("*", "matchEventIndicator"): Rule(
        "Required. Absent: flags 0, and the message neither opens nor closes an event (3.5)"),
    ("*", "transactTime"): Rule("Absent (before v15 on most templates) reads 0: exch_time 0 on the record "
                                "or END_OF_EVENT marker"),
    ("*", "mDEntryTimestamp"): Rule("Absent reads 0: exch_time 0 on the record or END_OF_EVENT marker"),
    ("*", "origTime"): Rule("Absent reads 0: exch_time 0 on an END_OF_EVENT marker"),
}
GENERIC = tuple(f for t, f in RULES if t == "*")


@dataclass(frozen=True)
class Row:
    template: str
    template_id: int
    field: str
    presence: str
    null: object
    since: object
    primitive: str
    offset: int
    size: int
    rule: Rule


def schema():
    return gen_layouts.load_schema(gen_layouts.XML_PATH)


def rows(sch=None):
    """Every audited (template, field), in template id then field order."""
    sch = sch or schema()
    out = []
    for t in sch["templates"]:
        fields = {f["name"]: f for f in t["fields"]}
        for name, f in fields.items():
            rule = RULES.get((t["name"], name)) or (RULES.get(("*", name)) if name in GENERIC else None)
            if rule is None:
                continue
            out.append(Row(t["name"], t["id"], name, f["presence"], f["null"], f["since_version"],
                           f["primitive"], f["offset"], f["size"], rule))
    return out


def unknown_rules(sch=None):
    """Declared (template, field) pairs the schema does not have."""
    sch = sch or schema()
    have = {(t["name"], f["name"]) for t in sch["templates"] for f in t["fields"]}
    names = {f["name"] for t in sch["templates"] for f in t["fields"]}
    return sorted(k for k in RULES if (k[0] != "*" and k not in have) or (k[0] == "*" and k[1] not in names))


# ---- The C++ reads, from the source ----
READ = re.compile(r"(?:value_or_null|decode::get)<(?:T|F::(\w+)|fields::(\w+))::(\w+)>")
BLOCK = re.compile(r"Id == L::(\w+)::template_id|TemplateId == L::(\w+)::template_id")
NAMED = {"kSecurityID": "securityID", "kRptSeq": "rptSeq", "kMatchEventIndicator": "matchEventIndicator"}


def cpp_reads(sources=SOURCES, sch=None):
    """{(template, field)} the C++ reads: explicit reads (value_or_null<T::f>,
    decode::get<fields::X::f>) and named reads (securityID, rptSeq,
    matchEventIndicator, the time field), expanded to every template with
    the field."""
    sch = sch or schema()
    reads = set()
    for path in sources:
        current = None
        for line in Path(path).read_text().splitlines():
            m = BLOCK.search(line)
            if m:
                current = m.group(1) or m.group(2)
            for r in READ.finditer(line):
                tpl = r.group(1) or r.group(2) or current
                if tpl:
                    reads.add((tpl, r.group(3)))
        text = Path(path).read_text()
        for key, field in NAMED.items():
            if re.search(r"get_named<" + key + r">", text):
                reads |= {(t["name"], field) for t in sch["templates"] if any(f["name"] == field for f in t["fields"])}
        if "TimeField<" in text:
            for t in sch["templates"]:
                names = [f["name"] for f in t["fields"]]
                for tf in TIME_FIELDS:
                    if tf in names:
                        reads.add((t["name"], tf))
                        break
    return reads


def problems(sources=SOURCES, rules=None, sch=None):
    """Everything test_field_audit.py fails on: a field the C++ reads with no
    row, a row with no rule text, a rule naming a field the schema lacks,
    an unappliable reason the tracker does not know."""
    sch = sch or schema()
    saved = dict(RULES)
    if rules is not None:
        RULES.clear()
        RULES.update(rules)
    try:
        table = rows(sch)
        have = {(r.template, r.field) for r in table}
        out = [f"{t}.{f} is read by the C++ but has no row in the audit" for t, f in sorted(cpp_reads(sources, sch) - have)]
        out += [f"{r.template}.{r.field} has no rule" for r in table if not r.rule.text.strip()]
        out += [f"rule for {t}.{f}: no such field in the schema" for t, f in unknown_rules(sch)]
        tracker = (REPO / "cpp_baseline" / "include" / "b3" / "tracker.hpp").read_text()
        out += [f"{r.template}.{r.field}: unappliable reason {r.rule.reason!r} unknown to tracker.hpp"
                for r in table if r.rule.reason and f'"{r.rule.reason}"' not in tracker]
        return out
    finally:
        RULES.clear()
        RULES.update(saved)


# ---- Python counts from intent rows (counters_compare) ----
def counts(intent_rows, sch=None):
    """{"Template.field": [absent, null, invalid]} over intent-shaped rows."""
    sch = sch or schema()
    table = rows(sch)
    by_id = {}
    for i, r in enumerate(table):
        by_id.setdefault(r.template_id, []).append((i, r))
    out = [[0, 0, 0] for _ in table]
    for row in intent_rows:
        for i, r in by_id.get(row["templateId"], ()):
            if r.rule.when:
                action = row["fields"].get("mDUpdateAction", 0) if "mDUpdateAction" not in row.get("absent", []) else 0
                if action not in [ACTION[a] for a in r.rule.when]:
                    continue
            if r.field in row.get("absent", []):
                out[i][0] += 1
            elif r.field in row.get("null", []):
                out[i][1] += 1
            elif r.rule.side and row["fields"].get(r.field) not in ("0", "1"):
                out[i][2] += 1
    return {f"{r.template}.{r.field}": v for r, v in zip(table, out)}


# ---- Rendering ----
def fmt_null(r):
    if r.presence != "optional":
        return "–"
    if r.null == -(1 << 63):
        return "INT64_MIN"
    return str(r.null)


def render_table(sch=None):
    out = ["| Template | Field | Presence | Null | Since | Read when | Rule (absent or null) |",
           "|---|---|---|---|---|---|---|"]
    for r in rows(sch):
        when = "/".join(r.rule.when) if r.rule.when else "always"
        since = f"v{r.since}" if r.since else "–"
        out.append(f"| {r.template} | {r.field} | {r.presence} | {fmt_null(r)} | {since} | {when} | {r.rule.text} |")
    return "\n".join(out) + "\n"


def render_design(text, sch=None):
    if BEGIN not in text or END not in text:
        raise SystemExit(f"{DESIGN}: markers {BEGIN} / {END} not found")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return head + BEGIN + "\n" + render_table(sch) + END + tail


def render_header(sch=None):
    table = rows(sch)
    w = ["// field_audit.hpp: the audited fields (docs/DESIGN.md 3.10), and their counts.",
         "//",
         "// GENERATED by tools/field_audit.py from its rules and the parsed schema.",
         "// Do not edit by hand: change the rules there and run make field-audit.",
         "//",
         "// What it does:",
         "//   count<Id>(m, c): for each audited field of template Id that the C++",
         "//   reads on this message (an Order_MBO's by mDUpdateAction), counts it",
         "//   absent, null, or (side fields) invalid. The rules themselves are",
         "//   applied where the fields are used (event_sink.hpp, tracker.hpp).",
         "//",
         "// What it must not do:",
         "//   Allocate, or change any state but the counts.",
         "",
         "#pragma once",
         "",
         "#include <array>",
         "#include <cstddef>",
         "#include <cstdint>",
         "#include <string_view>",
         "",
         '#include "b3/decode.hpp"',
         '#include "b3/fields.hpp"',
         '#include "b3/layouts.hpp"',
         '#include "b3/walker.hpp"',
         "",
         "namespace b3::field_audit {",
         "",
         f"inline constexpr std::size_t kRows = {len(table)};",
         "inline constexpr std::array<std::string_view, kRows> kRowNames = {",
         ]
    for r in table:
        w.append(f'    "{r.template}.{r.field}",')
    w += ["};",
          "",
          "// Where each row's field sits, for tools that build or damage messages",
          "// (apps/b3_fuzz.cpp): offset and size in the root block, the null value's",
          "// little-endian bits (optional fields), the version it appeared in (0: always).",
          "struct RowInfo {",
          "    std::uint16_t template_id;",
          "    std::uint16_t offset;",
          "    std::uint8_t size;",
          "    bool optional;",
          "    std::uint64_t null_bits;",
          "    std::uint16_t since_version;",
          "    bool side;",
          "};",
          "inline constexpr std::array<RowInfo, kRows> kRowInfo = {{"]
    for r in table:
        bits = (r.null & ((1 << (8 * min(r.size, 8))) - 1)) if r.presence == "optional" and isinstance(r.null, int) else 0
        w.append(f"    {{{r.template_id}, {r.offset}, {r.size}, {'true' if r.presence == 'optional' else 'false'}, "
                 f"0x{bits:x}ULL, {r.since or 0}, {'true' if r.rule.side else 'false'}}},   // {r.template}.{r.field}")
    w += ["}};",
          "",
          "struct Counts {",
          "    std::array<std::uint64_t, kRows> absent{};",
          "    std::array<std::uint64_t, kRows> null{};",
          "    std::array<std::uint64_t, kRows> invalid{};   // side fields: not '0' or '1'",
          "};",
          "",
          "namespace detail {",
          "template <class V>",
          "inline void tally(Counts& c, std::size_t row, const V& v) noexcept {",
          "    if (v.absent()) ++c.absent[row];",
          "    else if (v.null()) ++c.null[row];",
          "}",
          "template <class V>",
          "inline void tally_side(Counts& c, std::size_t row, const V& v) noexcept {",
          "    if (v.absent()) ++c.absent[row];",
          "    else if (v.null()) ++c.null[row];",
          "    else if (v.value != '0' && v.value != '1') ++c.invalid[row];",
          "}",
          "}  // namespace detail",
          "",
          "template <std::uint16_t Id>",
          "inline void count(const walker::MessageView& m, Counts& c) noexcept {",
          "    namespace F = fields;",
          "    namespace L = layout;"]
    by_tpl = {}
    for i, r in enumerate(table):
        by_tpl.setdefault((r.template_id, r.template), []).append((i, r))
    first = True
    for (tid, tname), items in sorted(by_tpl.items()):
        w.append(f"    {'if' if first else '} else if'} constexpr (Id == L::{tname}::template_id) {{")
        first = False
        if any(r.rule.when for _, r in items):
            w.append(f"        const auto action_v = decode::get<F::{tname}::mDUpdateAction>(m);")
            w.append("        const auto action = action_v.absent() ? 0 : static_cast<unsigned>(action_v.value);")
        for i, r in items:
            fn = "tally_side" if r.rule.side else "tally"
            call = f"detail::{fn}(c, {i}, decode::get<F::{tname}::{r.field}>(m));"
            if r.rule.when:
                cond = " || ".join(f"action == {ACTION[a]}" for a in r.rule.when)
                w.append(f"        if ({cond}) {call}   // {'/'.join(r.rule.when)}")
            else:
                w.append(f"        {call}")
    w += ["    } else {",
          "        (void)m;",
          "        (void)c;",
          "    }",
          "}",
          "",
          "}  // namespace b3::field_audit",
          ""]
    return "\n".join(w)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if a generated file is out of date")
    args = ap.parse_args(argv)
    sch = schema()
    bad = problems(sch=sch)
    if bad:
        for p in bad:
            print(f"error: {p}", file=sys.stderr)
        return 2
    design = render_design(DESIGN.read_text(encoding="utf-8"), sch)
    header = render_header(sch)
    if args.check:
        stale = [p for p, want in ((DESIGN, design), (HEADER, header))
                 if not p.is_file() or p.read_text(encoding="utf-8") != want]
        for p in stale:
            print(f"{p.relative_to(REPO)} is out of date: run make field-audit", file=sys.stderr)
        return 1 if stale else 0
    DESIGN.write_text(design, encoding="utf-8")
    HEADER.write_text(header, encoding="utf-8")
    print(f"{len(rows(sch))} audited fields: {DESIGN.relative_to(REPO)} 3.10, {HEADER.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
