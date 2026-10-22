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
from pathlib import Path

import pytest

from b3gen import layouts
from layouts_md import LAYOUTS_ENUMS, LAYOUTS_SETS, MESSAGE_LAYOUTS, TEMPLATE_INDEX

FEED_GENERATOR = Path(__file__).resolve().parents[1]


def load_generator():
    path = FEED_GENERATOR / "tools" / "gen_layouts.py"
    spec = importlib.util.spec_from_file_location("gen_layouts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen_layouts = load_generator()


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
