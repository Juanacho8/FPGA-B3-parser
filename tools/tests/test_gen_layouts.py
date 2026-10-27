"""gen_layouts: both outputs come from one run and match the checked-in files;
the SHA-256 guard reads spec/LAYOUTS.md; the C++ target rejects bad names."""

import importlib.util
import subprocess
import sys

import pytest

from conftest import REPO, TOOLS


def load():
    spec = importlib.util.spec_from_file_location("gen_layouts", TOOLS / "gen_layouts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = load()


def test_checked_in_outputs_are_current():
    outputs = gen.render_all()
    assert set(outputs) == {gen.PY_OUT, gen.CPP_OUT, gen.FIELDS_OUT}
    for rel, text in outputs.items():
        assert (REPO / rel).read_bytes() == text.encode("utf-8"), (
            f"{rel} is stale or hand-edited; run make layouts")


def test_out_dir_writes_both_files(tmp_path):
    subprocess.run([sys.executable, str(TOOLS / "gen_layouts.py"), "--out-dir", str(tmp_path)],
                   check=True, capture_output=True)
    for rel in (gen.PY_OUT, gen.CPP_OUT, gen.FIELDS_OUT):
        assert (tmp_path / rel).read_bytes() == (REPO / rel).read_bytes()


def test_expected_sha_comes_from_layouts_md(tmp_path):
    assert gen.expected_sha256() == \
        "b055b9a948756ab795f0f6f645a571094cacb85133e091e94440b812e17503c4"
    other = tmp_path / "LAYOUTS.md"
    other.write_text("# x\n\nXML SHA-256: `" + "0" * 64 + "`\n")
    assert gen.expected_sha256(other) == "0" * 64
    missing = tmp_path / "EMPTY.md"
    missing.write_text("no hash here\n")
    with pytest.raises(gen.SchemaError):
        gen.expected_sha256(missing)


def test_refuses_xml_that_does_not_match(tmp_path):
    tampered = tmp_path / "schema.xml"
    tampered.write_bytes(gen.XML_PATH.read_bytes() + b"\n")
    with pytest.raises(gen.SchemaError, match="Refusing"):
        gen.render_all(tampered)


@pytest.mark.parametrize("name", ["class", "int", "and", "1abc", "a b", "x__y", "block_length"])
def test_cpp_names_are_checked(name):
    with pytest.raises(gen.SchemaError):
        gen.cpp_name(name, "test", reserved={"block_length"})


def test_cpp_literals():
    assert gen.cpp_literal(-(1 << 63), "int64") == "std::numeric_limits<std::int64_t>::min()"
    assert gen.cpp_literal((1 << 64) - 1, "uint64") == "18446744073709551615ull"
    assert gen.cpp_literal(255, "uint8") == "255u"
    assert gen.cpp_literal("X", "char") == "'X'"
    assert gen.cpp_literal(0, "char") == "0"
    with pytest.raises(gen.SchemaError):
        gen.cpp_literal(256, "uint8")
