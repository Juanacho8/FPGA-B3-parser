"""Support: config file loading.

Instruments, channels, securityIDs, ticks, multicast addresses, seed,
message targets, packet window, heartbeat interval, regime and flow
parameters, Feed B delay and loss/gap/idle parameters, all from one TOML file
(feed_generator/config.toml is the default and documents every value).

Validation is strict: unknown keys, missing keys, wrong types and
inconsistent values (an instrument on a channel with no multicast groups, a
price off its tick, ...) raise ConfigError naming the file and the key path,
e.g. "config.toml: instruments[2].tick: expected int, got str ('100')".

Must not: hold logic beyond reading and validating settings.
See docs/FEED_GENERATOR_CONTEXT.md section 10 (Config).
"""

import dataclasses
import datetime
import hashlib
import ipaddress
import tomllib
from dataclasses import dataclass
from pathlib import Path

from b3gen import scenarios
from b3gen.flow import FlowParams
from b3gen.main import InstrumentConfig, RunConfig, _band

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.toml"


class ConfigError(ValueError):
    """A config file that cannot be used, with the key path at fault."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class Opt:
    """An optional key."""

    def __init__(self, spec):
        self.spec = spec


class ListOf:
    def __init__(self, spec):
        self.spec = spec


class MapOf:
    """A table whose keys are data (channel numbers), all with one spec."""

    def __init__(self, key, spec):
        self.key, self.spec = key, spec


GROUP = {"group": str, "port": int}
BURST = {"burst_rate": float, "mean_burst": float}

SCHEMA = {
    "seed": int,
    "session_date": datetime.date,
    "session_open_utc": str,
    "sets": {"small_messages": int, "large_messages": int, "burst_messages": int,
             "burst_spike_every_ns": int, "scenario_messages": int, "book_key_every": int},
    "packetizer": {"window_ns": int, "heartbeat_ns": int},
    "network": {"src_ip": str, "src_port": int},
    "channels": MapOf("channel number", {"feed_a": GROUP, "feed_b": GROUP}),
    "instruments": ListOf({
        "name": str, "security_id": int, "channel": int, "segment": str,
        "security_group": str, "tick": int, "lot": int, "is_future": bool,
        "prev_close": int, "band_bps": int, "avg_daily_traded_qty": int,
        "max_trade_vol": int, "settlement_price": Opt(int), "open_interest": Opt(int),
        "rates": {"calm": float, "busy": float, "spike": float}}),
    "flow": {
        "calm_mean_ns": int, "busy_mean_ns": int, "spike_mean_ns": int, "spike_every_ns": int,
        "p_new": float, "p_cancel": float, "p_size": float, "p_price": float,
        "p_ioc": float, "p_market": float, "target_live_orders": int,
        "near_ticks": int, "cross_fraction": float, "cross_ticks": int,
        "lots_continue": float, "max_lots": int, "aggressive_max_lots": int,
        "price_change_ticks": int, "thin_levels": int, "thin_bias": float,
        "firm_count": int},
    "feed_b": {"base_delay_ns": int, "jitter_ns": int},
    "loss": BURST,
    "gap": BURST,
    "idle": {"duration_ns": int},
}

TYPE_NAMES = {int: "int", float: "float", bool: "bool", str: "str",
              datetime.date: "date", dict: "table", list: "array"}


def _type_name(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, datetime.datetime):
        return "datetime"
    return TYPE_NAMES.get(type(value), type(value).__name__)


def _check(value, spec, path, fail):
    """Check value against spec; return it with floats normalized."""
    if isinstance(spec, Opt):
        return _check(value, spec.spec, path, fail)
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            fail(path, f"expected a table, got {_type_name(value)}")
        for key in value:
            if key not in spec:
                fail(_join(path, key), "unknown key")
        out = {}
        for key, sub in spec.items():
            if key not in value:
                if not isinstance(sub, Opt):
                    fail(_join(path, key), "missing")
                continue
            out[key] = _check(value[key], sub, _join(path, key), fail)
        return out
    if isinstance(spec, ListOf):
        if not isinstance(value, list):
            fail(path, f"expected an array of tables, got {_type_name(value)}")
        return [_check(v, spec.spec, f"{path}[{i}]", fail) for i, v in enumerate(value)]
    if isinstance(spec, MapOf):
        if not isinstance(value, dict):
            fail(path, f"expected a table, got {_type_name(value)}")
        out = {}
        for key, sub in value.items():
            try:
                number = int(key)
            except ValueError:
                fail(_join(path, key), f"key must be a {spec.key}")
            out[number] = _check(sub, spec.spec, _join(path, key), fail)
        return out
    ok = (isinstance(value, spec) and not isinstance(value, bool) if spec is int else
          isinstance(value, (int, float)) and not isinstance(value, bool) if spec is float else
          isinstance(value, datetime.date) and not isinstance(value, datetime.datetime)
          if spec is datetime.date else isinstance(value, spec))
    if not ok:
        fail(path, f"expected {TYPE_NAMES[spec]}, got {_type_name(value)} ({value!r})")
    return float(value) if spec is float else value


def _join(path, key):
    return f"{path}.{key}" if path else str(key)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SetSizes:
    small_messages: int
    large_messages: int
    burst_messages: int
    burst_spike_every_ns: int
    scenario_messages: int


@dataclass(frozen=True)
class Settings:
    """A validated config: run_config() makes the RunConfig for one run."""
    path: str
    text: str
    sha256: str
    base: RunConfig            # target_messages and out_dir are placeholders
    sets: SetSizes

    def run_config(self, out_dir, target_messages, seed=None, spike_every_ns=None):
        config = dataclasses.replace(self.base, out_dir=Path(out_dir),
                                     target_messages=target_messages)
        if seed is not None:
            config = dataclasses.replace(config, seed=seed)
        if spike_every_ns is not None:
            config = dataclasses.replace(config, flow=dataclasses.replace(
                config.flow, spike_every_ns=spike_every_ns))
        return config


def load(path=DEFAULT_PATH):
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ConfigError(f"{path}: cannot read: {e.strerror}") from e
    return loads(data.decode("utf-8"), str(path))


def loads(text, source="<config>"):
    def fail(key_path, message):
        raise ConfigError(f"{source}: {key_path}: {message}" if key_path else
                          f"{source}: {message}")

    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{source}: not valid TOML: {e}") from e
    c = _check(raw, SCHEMA, "", fail)
    _validate(c, fail)
    return Settings(path=source, text=text,
                    sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    base=_run_config(c), sets=SetSizes(**{
                        k: v for k, v in c["sets"].items() if k != "book_key_every"}))


def _validate(c, fail):
    def positive(path, value):
        if value <= 0:
            fail(path, f"must be > 0, got {value}")

    def probability(path, value):
        if not 0 <= value <= 1:
            fail(path, f"must be between 0 and 1, got {value}")

    try:
        datetime.time.fromisoformat(c["session_open_utc"])
    except ValueError:
        fail("session_open_utc", f"expected HH:MM, got {c['session_open_utc']!r}")
    for key, value in c["sets"].items():
        positive(f"sets.{key}", value)
    if c["packetizer"]["window_ns"] < 0:
        fail("packetizer.window_ns", "must be >= 0")
    positive("packetizer.heartbeat_ns", c["packetizer"]["heartbeat_ns"])
    _address("network.src_ip", c["network"]["src_ip"], multicast=False, fail=fail)
    _port("network.src_port", c["network"]["src_port"], fail)
    if not c["channels"]:
        fail("channels", "at least one channel is needed")
    for channel, feeds in c["channels"].items():
        if not 0 <= channel <= 255:
            fail(f"channels.{channel}", "channel numbers are uint8 (0..255)")
        for feed in ("feed_a", "feed_b"):
            _address(f"channels.{channel}.{feed}.group", feeds[feed]["group"], True, fail)
            _port(f"channels.{channel}.{feed}.port", feeds[feed]["port"], fail)
    if not c["instruments"]:
        fail("instruments", "at least one instrument is needed")
    names, ids = set(), set()
    for k, i in enumerate(c["instruments"]):
        where = f"instruments[{k}]"
        if i["name"] in names:
            fail(f"{where}.name", f"duplicate name {i['name']!r}")
        if i["security_id"] in ids:
            fail(f"{where}.security_id", f"duplicate securityID {i['security_id']}")
        names.add(i["name"])
        ids.add(i["security_id"])
        if i["channel"] not in c["channels"]:
            fail(f"{where}.channel", f"channel {i['channel']} has no [channels.{i['channel']}]")
        for key in ("security_id", "tick", "lot", "prev_close"):
            positive(f"{where}.{key}", i[key])
        if i["prev_close"] % i["tick"]:
            fail(f"{where}.prev_close", f"{i['prev_close']} is not a multiple of tick {i['tick']}")
        if not 0 < i["band_bps"] < 10_000:
            fail(f"{where}.band_bps", "must be between 1 and 9999")
        if len(i["security_group"]) > 3:
            fail(f"{where}.security_group", "at most 3 characters (SecurityGroup is char[3])")
        for key in ("settlement_price", "open_interest"):
            if i["is_future"] and key not in i:
                fail(f"{where}.{key}", "missing (required for a future)")
            if not i["is_future"] and key in i:
                fail(f"{where}.{key}", "only futures have it")
        for regime, rate in i["rates"].items():
            if rate < 0:
                fail(f"{where}.rates.{regime}", "must be >= 0")
    f = c["flow"]
    for key in ("calm_mean_ns", "busy_mean_ns", "spike_mean_ns", "spike_every_ns",
                "target_live_orders", "near_ticks", "max_lots", "aggressive_max_lots",
                "price_change_ticks", "firm_count"):
        positive(f"flow.{key}", f[key])
    for key in ("p_new", "p_cancel", "p_size", "p_price", "p_ioc", "p_market",
                "cross_fraction", "lots_continue", "thin_bias"):
        probability(f"flow.{key}", f[key])
    for key in ("base_delay_ns", "jitter_ns"):
        if c["feed_b"][key] < 0:
            fail(f"feed_b.{key}", "must be >= 0")
    for section in ("loss", "gap"):
        probability(f"{section}.burst_rate", c[section]["burst_rate"])
        if c[section]["mean_burst"] < 1:
            fail(f"{section}.mean_burst", "must be >= 1")
    positive("idle.duration_ns", c["idle"]["duration_ns"])


def _address(path, value, multicast, fail):
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        fail(path, f"not an IPv4 address: {value!r}")
    if multicast and not address.is_multicast:
        fail(path, f"{value} is not a multicast address")


def _port(path, value, fail):
    if not 1 <= value <= 65535:
        fail(path, f"port must be 1..65535, got {value}")


def _run_config(c):
    start = datetime.datetime.combine(
        c["session_date"], datetime.time.fromisoformat(c["session_open_utc"]),
        tzinfo=datetime.timezone.utc)
    instruments = []
    for i in c["instruments"]:
        low, high = _band(i["prev_close"], i["band_bps"], i["tick"])
        instruments.append(InstrumentConfig(
            i["name"], i["security_id"], i["channel"], i["segment"], i["security_group"],
            i["tick"], i["lot"], i["is_future"], i["prev_close"], low, high,
            i["avg_daily_traded_qty"], i["max_trade_vol"], i.get("settlement_price"),
            i.get("open_interest"), i["band_bps"]))
    f = dict(c["flow"])
    firms = tuple(range(1, f.pop("firm_count") + 1))
    rates = {i["name"]: tuple(_number(i["rates"][r]) for r in ("calm", "busy", "spike"))
             for i in c["instruments"]}
    flow = FlowParams(rates=rates, firms=firms, **f)
    groups = c["channels"]
    return RunConfig(
        seed=c["seed"], out_dir=Path("."), target_messages=1,
        instruments=tuple(instruments), flow=flow,
        start_ns=int(start.timestamp()) * 1_000_000_000,
        trade_date=(c["session_date"] - datetime.date(1970, 1, 1)).days,
        channel_map={ch: (g["feed_a"]["group"], g["feed_a"]["port"]) for ch, g in groups.items()},
        channel_map_b={ch: (g["feed_b"]["group"], g["feed_b"]["port"])
                       for ch, g in groups.items()},
        src_ip=c["network"]["src_ip"], src_port=c["network"]["src_port"],
        window_ns=c["packetizer"]["window_ns"], heartbeat_ns=c["packetizer"]["heartbeat_ns"],
        book_key_every=c["sets"]["book_key_every"],
        feed_b=scenarios.FeedBParams(**c["feed_b"]),
        loss=scenarios.BurstParams(**c["loss"]), gap=scenarios.BurstParams(**c["gap"]),
        idle_ns=c["idle"]["duration_ns"])


def _number(x):
    """Rates: keep whole numbers as int, so the flow sees what default_config gives."""
    return int(x) if float(x).is_integer() else x
