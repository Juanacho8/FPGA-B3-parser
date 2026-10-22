"""Parse spec/LAYOUTS.md, an independent source for the wire format.

Shared by the layout and encoder tests. LAYOUTS.md was verified against B3's
Message Reference PDF, so tests compare our code against a second source.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAYOUTS_MD = (REPO / "spec" / "LAYOUTS.md").read_text(encoding="utf-8")


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
