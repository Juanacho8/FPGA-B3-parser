"""gen_instruments: the instrument table from config.toml."""

import pytest

import gen_instruments as gi
from conftest import REPO


def test_from_the_generator_config(tmp_path):
    out = tmp_path / "instruments.txt"
    assert gi.main(["-o", str(out)]) == 0
    lines = [ln for ln in out.read_text().splitlines() if ln and not ln.startswith("#")]
    assert lines == ["200000001 68  # WIN", "200000002 68  # DOL",
                     "100000001 70  # PETR4", "100000002 70  # VALE3"]
    assert (REPO / "feed_generator" / "config.toml").is_file()


def test_bad_config(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[[instruments]]\nname = "X"\nsecurity_id = "1"\nchannel = 68\n')
    with pytest.raises(SystemExit, match="integer security_id"):
        gi.table_text(cfg)
    cfg.write_text("seed = 1\n")
    with pytest.raises(SystemExit, match="no \\[\\[instruments\\]\\]"):
        gi.table_text(cfg)
