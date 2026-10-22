"""Config file loading: the default file, strict validation."""

import pytest

from b3gen import config, main

TINY = config.DEFAULT_PATH.parent / "tests" / "fixtures" / "tiny_config.toml"


def test_default_file_is_default_config():
    settings = config.load()
    for target in (20_000, 6_000):
        assert settings.run_config("out", target) == \
            main.default_config(seed=1, target_messages=target, out_dir="out")


def test_overrides():
    settings = config.load()
    c = settings.run_config("o", 10, seed=9, spike_every_ns=50_000_000)
    assert (c.seed, c.target_messages, c.flow.spike_every_ns) == (9, 10, 50_000_000)
    assert settings.base.seed == 1  # the loaded settings are not changed


def test_every_value_has_a_comment_and_a_label():
    # Every key line in the default file explains itself, and the tags used
    # are the documented ones.
    labels = ("[B3]", "[B3 market]", "[inferred]", "[invented]", "[stress]")
    section_label = False
    for line in config.DEFAULT_PATH.read_text().splitlines():
        code = line.split("#")[0].strip()
        if not code:
            continue
        if code.startswith("["):
            section_label = any(tag in line for tag in labels) and "all [" in line
            continue
        comment = line.split("#", 1)[1] if "#" in line else ""
        assert section_label or any(tag in comment for tag in labels), line


def bad(text_edit):
    text = TINY.read_text()
    old, new = text_edit
    assert old in text, old
    return text.replace(old, new, 1)


CASES = [
    (("seed = 1", "seed = 1\ncolour = 3"), "colour: unknown key"),
    (("seed = 1", "seed = \"one\""), "seed: expected int, got str ('one')"),
    (("seed = 1", "seed = true"), "seed: expected int, got bool"),
    (("small_messages = 600", ""), "sets.small_messages: missing"),
    (("window_ns = 5_000", "window_ns = 5.5"), "packetizer.window_ns: expected int, got float"),
    (("src_ip = \"10.0.0.1\"", "src_ip = \"10.0.0\""), "network.src_ip: not an IPv4 address"),
    (("feed_a = { group = \"233.252.8.68\"", "feed_a = { group = \"10.1.1.1\""),
     "channels.68.feed_a.group: 10.1.1.1 is not a multicast address"),
    (("port = 30068", "port = 70000"), "channels.68.feed_a.port: port must be 1..65535"),
    (("tick = 50_000", "tick = \"50000\""), "instruments[0].tick: expected int, got str"),
    (("channel = 70", "channel = 71"), "instruments[1].channel: channel 71 has no [channels.71]"),
    (("prev_close = 385_000", "prev_close = 385_050"),
     "instruments[1].prev_close: 385050 is not a multiple of tick 100"),
    (("security_id = 100_000_001", "security_id = 200_000_001"),
     "instruments[1].security_id: duplicate securityID"),
    (("open_interest = 450_000", ""), "instruments[0].open_interest: missing (required for a future)"),
    (("max_trade_vol = 10_000_000", "max_trade_vol = 10_000_000\nopen_interest = 5"),
     "instruments[1].open_interest: only futures have it"),
    (("spike = 960_000 }", "spike = 960_000, storm = 1 }"), "instruments[0].rates.storm: unknown key"),
    (("p_new = 0.55", "p_new = 1.5"), "flow.p_new: must be between 0 and 1"),
    (("mean_burst = 3.0", "mean_burst = 0.5"), "loss.mean_burst: must be >= 1"),
    (("session_open_utc = \"13:00\"", "session_open_utc = \"1pm\""),
     "session_open_utc: expected HH:MM"),
    (("session_date = 2026-10-19", "session_date = \"2026-10-19\""),
     "session_date: expected date, got str"),
    (("[idle]", "[idle]\n[extra]"), "extra: unknown key"),
    (("seed = 1", "seed = = 1"), "not valid TOML"),
]


@pytest.mark.parametrize("edit,message", CASES, ids=[c[1].split(":")[0] for c in CASES])
def test_bad_configs_name_the_key(edit, message):
    with pytest.raises(config.ConfigError) as e:
        config.loads(bad(edit), "tiny.toml")
    assert str(e.value).startswith("tiny.toml: ")
    assert message in str(e.value), str(e.value)


def test_missing_file():
    with pytest.raises(config.ConfigError, match="cannot read"):
        config.load("/nonexistent/config.toml")
