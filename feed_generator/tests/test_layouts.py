"""Checks for the generated b3gen/layouts.py.

1. Regenerating from the XML gives exactly the checked-in file.
2. blockLength of all 29 templates matches spec/LAYOUTS.md section 6.
3. Field offsets and sizes (root block and group entries) and var data
   prefix sizes match spec/LAYOUTS.md section 7 for all 29 templates.
4. Every optional field's null value matches the Null column of section 7.
   Required fields are skipped: their null is None by design.
5. ENUMS and SETS match spec/LAYOUTS.md section 10.
6. The generator refuses an XML whose SHA-256 differs from the verified one.

spec/LAYOUTS.md was verified independently against B3's Message Reference
PDF, so these tests compare the generator against a second source.
"""

import importlib.util
import re
from pathlib import Path

import pytest

from b3gen import layouts

FEED_GENERATOR = Path(__file__).resolve().parents[1]
REPO = FEED_GENERATOR.parent
LAYOUTS_MD = (REPO / "spec" / "LAYOUTS.md").read_text(encoding="utf-8")


def load_generator():
    path = FEED_GENERATOR / "tools" / "gen_layouts.py"
    spec = importlib.util.spec_from_file_location("gen_layouts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen_layouts = load_generator()


def section(text, start, end):
    """Text between two '## N.' headings of LAYOUTS.md."""
    return text.split(start, 1)[1].split(end, 1)[0]


# ---------------------------------------------------------------------------
# Parse spec/LAYOUTS.md
# ---------------------------------------------------------------------------

def parse_template_index():
    """Section 6 table -> {template id: blockLength}. '36 + var data' -> 36."""
    index = {}
    for line in section(LAYOUTS_MD, "## 6. Template index", "## 7.").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].isdigit():
            index[int(cells[0])] = int(re.match(r"\d+", cells[2]).group())
    return index


TABLE_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|")
# Full section 7 row: Off | Size | Field | Tag | Type | P | Null | Notes.
ROW_CELLS = 8
GROUP_LINE = re.compile(r"^Group \*\*(\w+)\*\* .* entry blockLength \*\*(\d+)\*\*")
VAR_DATA_LINE = re.compile(r"^Var data \*\*(\w+)\*\* \(\d+\): (\d+)-byte")


def parse_message_layouts():
    """Section 7 -> {template id: {"fields": set, "groups": {name: (bl, set)},
    "var_data": {name: prefix size}, "optional_nulls": {path: null}}}.
    Field sets hold (name, offset, size); padding rows are skipped.
    optional_nulls is keyed by field name, or "group.field" inside groups,
    for rows whose P column is O. A blank Null cell is None."""
    layouts_by_id = {}
    body = section(LAYOUTS_MD, "## 7. Message layouts", "## 8.")
    for chunk in body.split("\n### ")[1:]:
        template_id = int(chunk.split()[0])
        entry = {"fields": set(), "groups": {}, "var_data": {}, "optional_nulls": {}}
        current = entry["fields"]
        prefix = ""
        for line in chunk.splitlines():
            group = GROUP_LINE.match(line)
            if group:
                current = set()
                prefix = group.group(1) + "."
                entry["groups"][group.group(1)] = (int(group.group(2)), current)
                continue
            var = VAR_DATA_LINE.match(line)
            if var:
                entry["var_data"][var.group(1)] = int(var.group(2))
                continue
            row = TABLE_ROW.match(line)
            if row and row.group(3) != "*padding*":
                current.add((row.group(3), int(row.group(1)), int(row.group(2))))
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                assert len(cells) == ROW_CELLS, line
                if cells[5] == "O":
                    entry["optional_nulls"][prefix + cells[2]] = int(cells[6]) if cells[6] else None
        layouts_by_id[template_id] = entry
    return layouts_by_id


ENUM_VALUE = re.compile(r"^(\w+) = ('?)(.+?)\2(?: \(since v\d+\))?$")
SET_BIT = re.compile(r"^bit (\d+) (\w+)(?: \(since v\d+\))?$")


def parse_enums_and_sets():
    """Section 10 table -> ({enum: {name: value}}, {set: {bit: name}}).
    Quoted values stay strings (char enums), others become ints."""
    enums, sets = {}, {}
    for line in section(LAYOUTS_MD, "## 10. Enums and sets", "## 11.").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3 or cells[0] in ("Name", "---"):
            continue
        name, encoding, values = cells
        if encoding.startswith("enum"):
            parsed = {}
            for item in values.split(", "):
                m = ENUM_VALUE.match(item)
                assert m, item
                parsed[m.group(1)] = m.group(3) if m.group(2) else int(m.group(3))
            enums[name] = parsed
        elif encoding.startswith("set"):
            parsed = {}
            for item in values.split(", "):
                m = SET_BIT.match(item)
                assert m, item
                parsed[int(m.group(1))] = m.group(2)
            sets[name] = parsed
    return enums, sets


TEMPLATE_INDEX = parse_template_index()
MESSAGE_LAYOUTS = parse_message_layouts()
LAYOUTS_ENUMS, LAYOUTS_SETS = parse_enums_and_sets()


def field_set(fields):
    return {(f.name, f.offset, f.size) for f in fields}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_regenerating_matches_checked_in_file():
    checked_in = (FEED_GENERATOR / "b3gen" / "layouts.py").read_text(encoding="utf-8")
    assert gen_layouts.render() == checked_in, (
        "b3gen/layouts.py is stale or was edited by hand. "
        "Run: python3 tools/gen_layouts.py"
    )


def test_all_29_templates_present():
    assert len(TEMPLATE_INDEX) == 29
    assert set(layouts.TEMPLATES) == set(TEMPLATE_INDEX)


@pytest.mark.parametrize("template_id", sorted(TEMPLATE_INDEX))
def test_block_length_matches_layouts_md(template_id):
    assert layouts.TEMPLATES[template_id].block_length == TEMPLATE_INDEX[template_id]


@pytest.mark.parametrize("template_id", sorted(MESSAGE_LAYOUTS))
def test_offsets_and_sizes_match_layouts_md(template_id):
    expected = MESSAGE_LAYOUTS[template_id]
    template = layouts.TEMPLATES[template_id]

    assert field_set(template.fields) == expected["fields"]

    groups = {g.name: (g.block_length, field_set(g.fields)) for g in template.groups}
    assert groups == expected["groups"]

    var_data = {d.name: d.prefix_size for d in template.var_data}
    assert var_data == expected["var_data"]


def optional_nulls(template):
    """{field path: null} for every optional field, same keys as the parser."""
    nulls = {f.name: f.null for f in template.fields if f.presence == "optional"}
    for g in template.groups:
        for f in g.fields:
            if f.presence == "optional":
                nulls[f"{g.name}.{f.name}"] = f.null
    return nulls


@pytest.mark.parametrize("template_id", sorted(MESSAGE_LAYOUTS))
def test_optional_null_values_match_layouts_md(template_id):
    # Same keys means presence agrees too: every field LAYOUTS.md marks O
    # is optional here, and no others.
    assert optional_nulls(layouts.TEMPLATES[template_id]) == MESSAGE_LAYOUTS[template_id]["optional_nulls"]


def test_layouts_md_has_optional_fields():
    # Guard against the parser silently finding nothing.
    assert sum(len(m["optional_nulls"]) for m in MESSAGE_LAYOUTS.values()) > 50


def test_enums_match_layouts_md():
    assert layouts.ENUMS == LAYOUTS_ENUMS


def test_sets_match_layouts_md():
    assert layouts.SETS == LAYOUTS_SETS


def test_layouts_md_covers_all_templates():
    assert set(MESSAGE_LAYOUTS) == set(TEMPLATE_INDEX)


def test_generator_refuses_wrong_sha(tmp_path):
    tampered = tmp_path / "schema.xml"
    tampered.write_bytes(gen_layouts.XML_PATH.read_bytes() + b"\n")
    with pytest.raises(gen_layouts.SchemaError, match="SHA-256"):
        gen_layouts.render(tampered)
