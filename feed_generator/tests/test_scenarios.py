"""Step 7a scenarios: Feed B, loss, gaps, idle.

Both pcaps are decoded independently (tests/wire.py, tests/sbe_decoder.py).
The clean scenario's Feed A is the full feed: every other scenario runs the
same session, so its packets are what the lossy feeds are checked against.
"""

import dataclasses
import json
from pathlib import Path

import pytest

from b3gen import main, scenarios
from b3gen.packet import Packetizer
from book_builder import BookBuilder
from layouts_md import MESSAGE_LAYOUTS
import sbe_decoder
from wire import read_packets, top5

TIMESTAMPS = ("transactTime", "mDEntryTimestamp", "mDInsertTimestamp", "aggressorTime")


def config(out_dir, **overrides):
    return main.default_config(seed=1, target_messages=6000, out_dir=out_dir,
                               book_key_every=50, **overrides)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    out = tmp_path_factory.mktemp("scenarios")
    return {name: main.run_scenario(config(out), name) for name in main.SCENARIOS}


def sequenced(packets):
    """{(channel, version, seq): packet} for real (non-heartbeat) packets."""
    out = {}
    for p in packets:
        if p["seq"] != 0:
            key = (p["channel"], p["version"], p["seq"])
            assert key not in out, f"{key} twice in one feed"
            out[key] = p
    return out


def heartbeats(packets):
    out = {}
    for p in packets:
        if p["seq"] == 0:
            out.setdefault(p["channel"], []).append(p)
    return out


def feeds(result):
    return read_packets(result.run.pcap_path), read_packets(result.feed_b_path)


def channel_map(config, feed):
    return {(group, port): channel for channel, (group, port) in
            (config.channel_map if feed == "A" else config.channel_map_b).items()}


# ---------------------------------------------------------------------------
# Feed B
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", main.SCENARIOS)
def test_feed_b_carries_the_same_bytes(runs, name):
    a, b = feeds(runs[name])
    a_seq, b_seq = sequenced(a), sequenced(b)
    for key in a_seq.keys() & b_seq.keys():
        assert a_seq[key]["payload"] == b_seq[key]["payload"], key
    a_hb, b_hb = heartbeats(a), heartbeats(b)
    assert a_hb.keys() == b_hb.keys()
    for channel in a_hb:
        assert [p["payload"] for p in a_hb[channel]] == [p["payload"] for p in b_hb[channel]]


@pytest.mark.parametrize("name", main.SCENARIOS)
def test_feed_b_is_never_earlier_and_keeps_channel_order(runs, name):
    result = runs[name]
    params = result.manifest["params"]["feed_b"]
    a, b = feeds(result)
    a_seq, b_seq = sequenced(a), sequenced(b)
    for key in a_seq.keys() & b_seq.keys():
        assert b_seq[key]["time"] >= a_seq[key]["time"] + params["base_delay_ns"], key
    a_hb, b_hb = heartbeats(a), heartbeats(b)
    for channel in a_hb:
        for pa, pb in zip(a_hb[channel], b_hb[channel]):
            assert pb["time"] >= pa["time"] + params["base_delay_ns"]
    assert [p["time"] for p in b] == sorted(p["time"] for p in b)
    for channel in {p["channel"] for p in b}:
        order = [(p["version"], p["seq"]) for p in b if p["channel"] == channel and p["seq"]]
        assert order == sorted(order)


def test_feed_b_has_its_own_multicast_groups(runs):
    import pcap_reader
    result = runs["clean"]
    for path, feed in ((result.run.pcap_path, "A"), (result.feed_b_path, "B")):
        _, records = pcap_reader.read_pcap(Path(path).read_bytes())
        groups = channel_map(main.default_config(), feed)
        for rec in records:
            channel = rec["payload"][0]
            assert groups[(rec["dst_ip"], rec["dst_port"])] == channel


def test_feed_b_jitter_is_spread_and_bounded(runs):
    a, b = feeds(runs["clean"])
    a_seq, b_seq = sequenced(a), sequenced(b)
    p = scenarios.FeedBParams()
    delays = [b_seq[k]["time"] - a_seq[k]["time"] - p.base_delay_ns for k in a_seq]
    # The channel-order clamp can push a delay past the jitter, rarely.
    within = sum(0 <= d <= p.jitter_ns for d in delays)
    assert within > 0.95 * len(delays)
    assert len(set(delays)) > 1000


# ---------------------------------------------------------------------------
# Loss and gaps
# ---------------------------------------------------------------------------

def missing(full, feed):
    """Sequenced keys of full absent from feed, in full's wire order."""
    present = sequenced(feed).keys()
    return [list(k) for k in sequenced(full) if k not in present]


@pytest.mark.parametrize("name", main.SCENARIOS)
def test_missing_packets_match_the_manifest(runs, name):
    full = read_packets(runs["clean"].run.pcap_path)
    a, b = feeds(runs[name])
    manifest = json.loads(Path(runs[name].manifest_path).read_text())
    assert manifest == runs[name].manifest
    assert missing(full, a) == manifest["missing"]["A"]
    assert missing(full, b) == manifest["missing"]["B"]
    expect = {"clean": (False, False), "idle": (False, False),
              "loss": (True, False), "gap": (True, True)}[name]
    assert (bool(manifest["missing"]["A"]), bool(manifest["missing"]["B"])) == expect
    if name == "gap":
        assert manifest["missing"]["A"] == manifest["missing"]["B"]


@pytest.mark.parametrize("name", main.SCENARIOS)
def test_heartbeats_are_never_dropped(runs, name):
    a, b = feeds(runs[name])
    count = runs[name].run.heartbeats
    assert sum(map(len, heartbeats(a).values())) == count
    assert sum(map(len, heartbeats(b).values())) == count


def bursts(keys):
    """Lengths of runs of consecutive sequence numbers per channel."""
    lengths = []
    previous = None
    for channel, version, seq in keys:
        if previous == (channel, version, seq - 1):
            lengths[-1] += 1
        else:
            lengths.append(1)
        previous = (channel, version, seq)
    return lengths


def test_loss_comes_in_bursts_at_the_configured_rate(runs):
    result = runs["loss"]
    lost = result.manifest["missing"]["A"]
    total = len(sequenced(read_packets(runs["clean"].run.pcap_path)))
    loss = main.default_config().loss
    # Each burst covers mean_burst packets on average, then the walk resumes.
    expected = loss.burst_rate * loss.mean_burst / (1 - loss.burst_rate + loss.burst_rate * loss.mean_burst)
    assert 0.6 * expected < len(lost) / total < 1.4 * expected, len(lost) / total
    lengths = bursts([tuple(k) for k in lost])
    assert max(lengths) > 1  # bursts, not isolated drops
    assert 0.6 * loss.mean_burst < sum(lengths) / len(lengths) < 1.6 * loss.mean_burst


def test_loss_and_gaps_have_their_own_rngs(tmp_path):
    # Feed B timing must not move which packets are lost, and the loss
    # parameters must not move the gaps.
    base = main.run_scenario(config(tmp_path / "a"), "loss").manifest["missing"]
    other_b = main.run_scenario(config(tmp_path / "b", feed_b=scenarios.FeedBParams(5_000, 77_000)),
                                "loss").manifest["missing"]
    assert other_b == base
    g1 = main.run_scenario(config(tmp_path / "c"), "gap").manifest
    g2 = main.run_scenario(config(tmp_path / "d", loss=scenarios.BurstParams(0.5, 9)), "gap").manifest
    assert g1["missing"] == g2["missing"] and g1["unreliable_books"] == g2["unreliable_books"]


def merged(a, b):
    """A/B arbitration: per channel, each sequence number once, in order."""
    by_key = sequenced(b)
    by_key.update(sequenced(a))
    return [by_key[k] for k in sorted(by_key)]


@pytest.mark.parametrize("name", ["loss", "gap"])
def test_merging_a_and_b_reproduces_the_feed(runs, name):
    full = sequenced(read_packets(runs["clean"].run.pcap_path))
    gaps = {tuple(k) for k in runs[name].manifest["missing"]["B"]}
    got = merged(*feeds(runs[name]))
    want = [full[k] for k in sorted(full) if k not in gaps]
    assert [p["payload"] for p in got] == [p["payload"] for p in want]
    if name == "loss":
        assert len(got) == len(full)


def replay(result, packets, skip=lambda key, security_id: False):
    """Rebuild books from packets (one channel after another, each in
    sequence order) and compare every checkpoint whose as_of is reached.
    skip(key, security_id) drops a message the client no longer trusts.
    Returns {instrument: checks}."""
    cfg = main.default_config()
    by_id = {i.name: i.security_id for i in cfg.instruments}
    expect = {}
    for row in [json.loads(line) for line in Path(result.run.book_key_path).read_text().splitlines()]:
        for name, book in row["books"].items():
            expect.setdefault(tuple(book["as_of"]), []).append((name, book))
    builder = BookBuilder()
    checks = {name: 0 for name in by_id}
    for p in packets:
        key = (p["channel"], p["version"], p["seq"])
        for index, data in enumerate(p["messages"]):
            _, fields = sbe_decoder.decode(data)
            if not skip(key, fields.get("securityID")):
                builder.apply(data)
            for name, book in expect.get(key + (index,), []):
                if not skip(key, by_id[name]):
                    assert top5(builder, by_id[name]) == (book["bids"], book["offers"]), (name, key)
                    checks[name] += 1
    return checks


def test_book_from_the_merged_feed_matches_the_key_under_loss(runs):
    result = runs["loss"]
    checks = replay(result, merged(*feeds(result)))
    rows = len(Path(result.run.book_key_path).read_text().splitlines())
    assert all(n == rows for n in checks.values()), (checks, rows)


def test_book_from_feed_a_alone_breaks_under_loss(runs):
    # The loss is real: without Feed B a client's book goes wrong.
    with pytest.raises((AssertionError, KeyError)):
        replay(runs["loss"], sorted(sequenced(feeds(runs["loss"])[0]).values(),
                                    key=lambda p: (p["channel"], p["version"], p["seq"])))


def test_unreliable_books_are_the_instruments_in_the_gaps(runs):
    result = runs["gap"]
    gaps = {tuple(k) for k in result.manifest["missing"]["B"]}
    cfg = main.default_config()
    on_channel = {}
    for i in cfg.instruments:
        on_channel.setdefault(i.channel, []).append(i.name)
    want = {}
    for row in [json.loads(line) for line in Path(result.run.intent_path).read_text().splitlines()]:
        key = (row["channel"], row["sequenceVersion"], row["sequenceNumber"])
        if key in gaps:
            names = on_channel[row["channel"]] if row["instrument"] is None else [row["instrument"]]
            for name in names:
                want.setdefault(name, list(key))
    assert result.manifest["unreliable_books"] == want
    assert want


def test_gap_books_match_the_key_where_reliable(runs):
    result = runs["gap"]
    cfg = main.default_config()
    name_of = {i.security_id: i.name for i in cfg.instruments}
    first_gap = {n: tuple(k) for n, k in result.manifest["unreliable_books"].items()}

    def skip(key, security_id):
        start = first_gap.get(name_of.get(security_id))
        return start is not None and key[0] == start[0] and key >= start

    checks = replay(result, merged(*feeds(result)), skip)
    rows = len(Path(result.run.book_key_path).read_text().splitlines())
    for name, n in checks.items():
        if name in first_gap:
            assert 0 < n < rows, (name, n)  # checked up to its first gap only
        else:
            assert n == rows, (name, n)


def test_bursts_never_take_heartbeats():
    import random
    p = Packetizer([68], 0)
    for t in (0, 3_500_000_000, 7_000_000_000):  # idle gaps, so heartbeats
        p.add(t, 68, b"\x0c\x00" + bytes(10))
    packetized = p.finish(9_000_000_000)
    assert packetized.heartbeats
    everything = scenarios.burst_positions(packetized, scenarios.BurstParams(1.0, 1000),
                                           random.Random(0))
    real = {i for i, (_, seq) in enumerate(packetized.headers) if seq}
    assert everything == real


class _Msg:
    def __init__(self, instrument):
        self.instrument = instrument


def test_message_without_instrument_marks_the_whole_channel():
    p = Packetizer([68, 70], 0, window_ns=0)
    published = []
    for t, channel, instrument in [(1, 68, "WIN"), (2, 68, None), (3, 70, "PETR4"),
                                   (4, 68, "DOL"), (5, 70, "VALE3")]:
        p.add(t, channel, b"\x0c\x00" + bytes(10), tag=len(published))
        published.append(_Msg(instrument))
    packetized = p.finish(10)
    on_channel = {68: ["WIN", "DOL"], 70: ["PETR4", "VALE3"]}
    position = {tag: pl.packet_position for tag, pl in packetized.placements.items()}
    # Gap on the SecurityGroupPhase-like packet, then on PETR4 and DOL.
    out = scenarios.unreliable_books(sorted([position[1], position[2], position[3]]),
                                     packetized, published, on_channel)
    assert out == {"WIN": (68, 1, 2), "DOL": (68, 1, 2), "PETR4": (70, 1, 1)}
    out = scenarios.unreliable_books([position[3]], packetized, published, on_channel)
    assert out == {"DOL": (68, 1, 3)}


# ---------------------------------------------------------------------------
# Idle
# ---------------------------------------------------------------------------

def test_idle_sends_heartbeats_on_both_feeds(runs):
    result = runs["idle"]
    idle = result.manifest["params"]["idle"]
    assert runs["clean"].run.heartbeats == 0 < result.run.heartbeats
    for packets in feeds(result):
        for channel, beats in heartbeats(packets).items():
            assert len(beats) == idle["duration_ns"] // 1_000_000_000, channel
            real = [p for p in packets if p["channel"] == channel and p["seq"]]
            for hb in beats:
                _, fields = sbe_decoder.decode(hb["messages"][0])
                before = [p for p in real if p["sending"] < hb["sending"]]
                after = [p for p in real if p["sending"] > hb["sending"]]
                assert before and after
                # The packet before the pause may leave up to a window late.
                assert after[0]["sending"] - before[-1]["sending"] >= idle["duration_ns"] - 5000
                assert fields["nextSeqNo"] == after[0]["seq"] == before[-1]["seq"] + 1


@pytest.mark.parametrize("name", ["clean", "idle"])
def test_sending_time_covers_every_timestamp_in_its_packet(runs, name):
    nulls_by_template = {t: MESSAGE_LAYOUTS[t]["optional_nulls"] for t in MESSAGE_LAYOUTS}
    longest = 0
    previous = {}
    for p in read_packets(runs[name].run.pcap_path):
        if not p["seq"]:
            continue
        for data in p["messages"]:
            header, fields = sbe_decoder.decode(data)
            nulls = nulls_by_template[header["templateId"]]
            for f in TIMESTAMPS:
                if f in fields and fields[f] != nulls.get(f):
                    assert fields[f] <= p["sending"], (p["channel"], p["seq"], f)
        if p["channel"] in previous:
            longest = max(longest, p["sending"] - previous[p["channel"]])
        previous[p["channel"]] = p["sending"]
    if name == "idle":
        assert longest >= runs[name].manifest["params"]["idle"]["duration_ns"] - 5000


def test_idle_moves_every_time_after_the_pause(runs):
    # Same session, so after the pause each message is the clean one with
    # every timestamp shifted by the idle duration.
    idle = runs["idle"].manifest["params"]["idle"]
    clean = [json.loads(line) for line in Path(runs["clean"].run.intent_path).read_text().splitlines()]
    quiet = [json.loads(line) for line in Path(runs["idle"].run.intent_path).read_text().splitlines()]
    clean = [r for r in clean if r["templateId"] != 2]
    quiet = [r for r in quiet if r["templateId"] != 2]
    assert len(clean) == len(quiet)
    shifted = 0
    for c, q in zip(sorted(clean, key=_order), sorted(quiet, key=_order)):
        assert c["templateId"] == q["templateId"]
        for f in ("transactTime", "mDEntryTimestamp"):
            if f in c["fields"] and f not in c["null"]:
                delta = q["fields"][f] - c["fields"][f]
                assert delta in (0, idle["duration_ns"]), (c, q)
                shifted += delta > 0
    assert shifted > 1000


def _order(row):
    return (row["channel"], row["sequenceVersion"], row["sequenceNumber"], row["index"])


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_scenarios_are_deterministic(tmp_path, runs):
    for name in ("loss", "gap", "idle"):
        again = main.run_scenario(config(tmp_path), name)
        for attr in ("pcap_path", "intent_path", "book_key_path"):
            assert Path(getattr(again.run, attr)).read_bytes() == \
                Path(getattr(runs[name].run, attr)).read_bytes(), (name, attr)
        assert Path(again.feed_b_path).read_bytes() == Path(runs[name].feed_b_path).read_bytes()
        assert again.manifest == runs[name].manifest


def test_unknown_scenario_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown scenario"):
        main.run_scenario(config(tmp_path), "nope")
