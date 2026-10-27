"""Step 7a scenarios: Feed B, loss, gaps, reorder, idle.

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
from replay import check_unreliable
from layouts_md import MESSAGE_LAYOUTS
import sbe_decoder
from wire import in_sequence_order, read_packets, top5

TIMESTAMPS = ("transactTime", "mDEntryTimestamp", "mDInsertTimestamp", "aggressorTime")


from conftest import scenario_config as config

FEED_SCENARIOS = ("clean", "loss", "gap", "gap_one", "reorder", "idle")  # same session as clean


def sequenced(packets):
    """{(channel, version, seq): packet} for real (non-heartbeat) packets."""
    out = {}
    for p in packets:
        if p["seq"]:  # not a heartbeat (0) or a stray datagram (None)
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
    # Apart from the reorder scenario's late packets (tested below).
    result = runs[name]
    params = result.manifest["params"]["feed_b"]
    late = late_keys(result, "A") | late_keys(result, "B")
    a, b = feeds(result)
    a_seq, b_seq = sequenced(a), sequenced(b)
    for key in a_seq.keys() & b_seq.keys() - late:
        assert b_seq[key]["time"] >= a_seq[key]["time"] + params["base_delay_ns"], key
    a_hb, b_hb = heartbeats(a), heartbeats(b)
    for channel in a_hb:
        for pa, pb in zip(a_hb[channel], b_hb[channel]):
            assert pb["time"] >= pa["time"] + params["base_delay_ns"]
    assert [p["time"] for p in b] == sorted(p["time"] for p in b)
    late_b = late_keys(result, "B")
    for channel in {p["channel"] for p in b}:
        order = [(p["version"], p["seq"]) for p in b if p["channel"] == channel and p["seq"]
                 and (channel, p["version"], p["seq"]) not in late_b]
        assert order == sorted(order)


def late_keys(result, feed):
    return {tuple(r["packet"]) for r in result.manifest["reordered"][feed]}


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
    """Keys of full (sequenced packet keys, wire order) absent from feed."""
    present = sequenced(feed).keys()
    return [list(k) for k in full if k not in present]


def published_packets(result):
    """Every sequenced packet the run published, in wire order, from its
    intent log (which describes everything published, whatever a feed lost)."""
    keys = []
    for line in Path(result.run.intent_path).read_text().splitlines():
        row = json.loads(line)
        key = (row["channel"], row["sequenceVersion"], row["sequenceNumber"])
        if row["sequenceNumber"] and (not keys or keys[-1] != key):
            keys.append(key)
    return keys


@pytest.mark.parametrize("name", main.SCENARIOS)
def test_missing_packets_match_the_manifest(runs, name):
    # The full feed is never the feed under test: the clean run's Feed A for
    # the scenarios that run the clean session, and for every scenario the
    # packets its intent log says were published.
    a, b = feeds(runs[name])
    full = published_packets(runs[name])
    if name in FEED_SCENARIOS:
        clean = list(sequenced(read_packets(runs["clean"].run.pcap_path)))
        assert clean == full  # same session as clean
    manifest = json.loads(Path(runs[name].manifest_path).read_text())
    assert manifest == runs[name].manifest
    assert missing(full, a) == manifest["missing"]["A"]
    assert missing(full, b) == manifest["missing"]["B"]
    expect = {"loss": (True, False), "gap": (True, True), "gap_one": (True, True)}.get(
        name, (False, False))
    assert (bool(manifest["missing"]["A"]), bool(manifest["missing"]["B"])) == expect
    if name in ("gap", "gap_one"):
        assert manifest["missing"]["A"] == manifest["missing"]["B"]
    if name == "gap_one":
        assert len(manifest["missing"]["B"]) == 1


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


@pytest.mark.parametrize("name", ["loss", "gap", "gap_one", "reorder"])
def test_merging_a_and_b_reproduces_the_feed(runs, name):
    full = sequenced(read_packets(runs["clean"].run.pcap_path))
    gaps = {tuple(k) for k in runs[name].manifest["missing"]["B"]}
    got = merged(*feeds(runs[name]))
    want = [full[k] for k in sorted(full) if k not in gaps]
    assert [p["payload"] for p in got] == [p["payload"] for p in want]
    if name in ("loss", "reorder"):
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


@pytest.mark.parametrize("name", ["gap", "gap_one"])
def test_unreliable_books_are_the_instruments_in_the_gaps(runs, name):
    # From the intent rows of the packets missing from both feeds: every
    # marked instrument lost a message there, and every lost book message
    # marks its instrument (tests/replay.py check_unreliable).
    result = runs[name]
    gaps = {tuple(k) for k in result.manifest["missing"]["B"]}
    lost = [row for row in (json.loads(line) for line in
                            Path(result.run.intent_path).read_text().splitlines())
            if (row["channel"], row["sequenceVersion"], row["sequenceNumber"]) in gaps]
    channel_of = {i.name: i.channel for i in main.default_config().instruments}
    assert result.manifest["unreliable_books"] and lost
    check_unreliable(result.manifest["unreliable_books"], lost, channel_of)


@pytest.mark.parametrize("name", ["gap", "gap_one"])
def test_gap_books_match_the_key_where_reliable(runs, name):
    result = runs[name]
    cfg = main.default_config()
    name_of = {i.security_id: i.name for i in cfg.instruments}
    first_gap = {n: tuple(k) for n, k in result.manifest["unreliable_books"].items()}

    def skip(key, security_id):
        start = first_gap.get(name_of.get(security_id))
        return start is not None and key[0] == start[0] and key >= start

    checks = replay(result, merged(*feeds(result)), skip)
    rows = len(Path(result.run.book_key_path).read_text().splitlines())
    for instrument, n in checks.items():
        if instrument in first_gap:
            assert 0 < n < rows, (instrument, n)  # checked up to its first gap only
        else:
            assert n == rows, (instrument, n)
    if name == "gap_one":
        # One packet, one instrument: the other three stay reliable to the end.
        assert len(first_gap) == 1
        assert sum(n == rows for n in checks.values()) == 3


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


def test_gap_one_really_breaks_its_instrument(runs):
    # Trusting the unreliable book past the gap must go wrong somewhere.
    result = runs["gap_one"]
    with pytest.raises((AssertionError, KeyError)):
        replay(result, merged(*feeds(result)))


CHANNEL_WIDE = {1, 10, 11}  # SequenceReset, SecurityGroupPhase, ChannelReset
NEWS = 5


class _Msg:
    def __init__(self, instrument, template_id):
        self.instrument = instrument
        self.template_id = template_id


def test_channel_wide_message_marks_the_whole_channel():
    p = Packetizer([68, 70], 0, window_ns=0)
    published = []
    for t, channel, instrument, template in [(1, 68, "WIN", 50), (2, 68, None, 10),
                                             (3, 70, "PETR4", 51), (4, 68, "DOL", 50),
                                             (5, 70, "VALE3", 53)]:
        p.add(t, channel, b"\x0c\x00" + bytes(10), tag=len(published))
        published.append(_Msg(instrument, template))
    packetized = p.finish(10)
    on_channel = {68: ["WIN", "DOL"], 70: ["PETR4", "VALE3"]}
    position = {tag: pl.packet_position for tag, pl in packetized.placements.items()}
    # Gap on the SecurityGroupPhase-like packet, then on PETR4 and DOL.
    out = scenarios.unreliable_books(sorted([position[1], position[2], position[3]]),
                                     packetized, published, on_channel)
    assert out == {"WIN": (68, 1, 2), "DOL": (68, 1, 2), "PETR4": (70, 1, 1)}
    out = scenarios.unreliable_books([position[3]], packetized, published, on_channel)
    assert out == {"DOL": (68, 1, 3)}


@pytest.mark.parametrize("template", [1, 10, 11])
def test_every_channel_wide_template_marks_the_channel(template):
    p = Packetizer([68], 0, window_ns=0)
    p.add(1, 68, b"\x0c\x00" + bytes(10), tag=0)
    packetized = p.finish(10)
    out = scenarios.unreliable_books([0], packetized, [_Msg(None, template)],
                                     {68: ["WIN", "DOL"]})
    assert out == {"WIN": (68, 1, 1), "DOL": (68, 1, 1)}


def test_news_and_unknown_templates_never_make_a_book_unreliable():
    p = Packetizer([68], 0, window_ns=0)
    published = [_Msg("WIN", NEWS), _Msg(None, NEWS), _Msg(None, 999)]
    for tag in range(3):
        p.add(tag, 68, b"\x0c\x00" + bytes(10), tag=tag)
    packetized = p.finish(10)
    assert scenarios.unreliable_books([0, 1, 2], packetized, published,
                                      {68: ["WIN", "DOL"]}) == {}


# ---------------------------------------------------------------------------
# Reorder
# ---------------------------------------------------------------------------

def late_from_the_wire(packets):
    """[{"packet", "after", "depth"}] read from arrival order alone: a packet
    is late when a higher sequence number of its channel arrived before it.
    after is the packet just before it on its channel, depth how many
    packets of its channel overtook it."""
    seen, previous, out = {}, {}, []
    for p in packets:
        if not p["seq"]:
            continue
        channel, key = p["channel"], (p["version"], p["seq"])
        ahead = [k for k in seen.get(channel, ()) if k > key]
        if ahead:
            out.append({"packet": [channel, *key], "after": [channel, *previous[channel]],
                        "depth": len(ahead)})
        seen.setdefault(channel, set()).add(key)
        previous[channel] = key
    return out


@pytest.mark.parametrize("feed", ["A", "B"])
def test_late_packets_match_the_manifest(runs, feed):
    result = runs["reorder"]
    packets = read_packets(result.run.pcap_path if feed == "A" else result.feed_b_path)
    assert late_from_the_wire(packets) == result.manifest["reordered"][feed]


@pytest.mark.parametrize("name", [n for n in main.SCENARIOS if n != "reorder"])
def test_only_the_reorder_scenario_reorders(runs, name):
    for packets in feeds(runs[name]):
        assert late_from_the_wire(packets) == []


def test_reorder_is_bounded_spread_and_independent_per_feed(runs):
    m = runs["reorder"].manifest
    max_depth = m["params"]["reorder"]["max_depth"]
    for feed in ("A", "B"):
        late = m["reordered"][feed]
        depths = [r["depth"] for r in late]
        assert len(late) >= 5 and set(depths) == set(range(1, max_depth + 1)), (feed, depths)
        assert len({r["packet"][0] for r in late}) > 1, "late packets on one channel only"
        for r in late:
            channel, version, seq = r["packet"]
            assert r["after"] == [channel, version, seq + r["depth"]], r
    assert late_keys(runs["reorder"], "A") != late_keys(runs["reorder"], "B")


def test_late_packets_take_the_time_of_the_packet_they_follow(runs):
    result = runs["reorder"]
    for feed, path in (("A", result.run.pcap_path), ("B", result.feed_b_path)):
        packets = read_packets(path)
        assert [p["time"] for p in packets] == sorted(p["time"] for p in packets)
        times = {(p["channel"], p["version"], p["seq"]): p["time"] for p in packets}
        for r in result.manifest["reordered"][feed]:
            assert times[tuple(r["packet"])] == times[tuple(r["after"])], (feed, r)
    # Only the arrival changes: the clean run's bytes, sendingTime included.
    clean = sequenced(read_packets(runs["clean"].run.pcap_path))
    a = sequenced(feeds(result)[0])
    assert {k: p["payload"] for k, p in a.items()} == {k: p["payload"] for k, p in clean.items()}


@pytest.mark.parametrize("feed", ["A", "B"])
def test_reordered_feed_in_sequence_order_matches_the_key(runs, feed):
    # A client with a reorder buffer loses nothing: no book is unreliable.
    result = runs["reorder"]
    assert result.manifest["unreliable_books"] == {}
    packets = read_packets(result.run.pcap_path if feed == "A" else result.feed_b_path)
    checks = replay(result, sorted(sequenced(in_sequence_order(packets)).values(),
                                   key=lambda p: (p["channel"], p["version"], p["seq"])))
    rows = len(Path(result.run.book_key_path).read_text().splitlines())
    assert all(n == rows for n in checks.values()), (checks, rows)


def test_reordered_feed_in_arrival_order_breaks_strict_sequencing(runs):
    # The reorder is real: applying packets as they arrive, the first late
    # packet's channel sees a jump.
    builder = BookBuilder()
    with pytest.raises(AssertionError):
        for p in read_packets(runs["reorder"].run.pcap_path):
            builder.apply_packet(p["channel"], p["version"], p["seq"], p["messages"])


def test_reorder_has_its_own_rngs(tmp_path):
    # Feed B timing and the loss parameters must not move which packets are late.
    base = main.run_scenario(config(tmp_path / "a"), "reorder").manifest["reordered"]
    other = main.run_scenario(config(tmp_path / "b", feed_b=scenarios.FeedBParams(5_000, 77_000),
                                     loss=scenarios.BurstParams(0.5, 9)),
                              "reorder").manifest["reordered"]
    assert other == base  # B too: jitter never reorders a channel, so its walk is the same


def _entries(keys):
    return [(i, key[0], bytes([i])) for i, key in enumerate(keys)], list(keys)


def test_reorder_never_crosses_a_version_change_or_overlaps():
    import random
    keys = [(68, 1, s) for s in range(1, 6)] + [(68, 2, s) for s in range(1, 6)] + \
        [(70, 1, s) for s in range(1, 30)]
    entries, keys = _entries(keys)
    for seed in range(50):
        out, moves = scenarios.reorder(entries, keys, scenarios.ReorderParams(0.5, 3),
                                       random.Random(seed))
        assert [e[0] for e in out] == sorted(e[0] for e in out)  # times never go back
        assert sorted(e[2] for e in out) == sorted(e[2] for e in entries)  # nothing lost
        late = [tuple(m["packet"]) for m in moves]
        spans = []
        for m in moves:
            channel, version, seq = m["packet"]
            assert m["after"] == [channel, version, seq + m["depth"]]  # same version
            assert 1 <= m["depth"] <= 3
            spans.append((channel, version, seq, seq + m["depth"]))
        for i, (c1, v1, lo1, hi1) in enumerate(spans):
            for c2, v2, lo2, hi2 in spans[i + 1:]:
                assert (c1, v1) != (c2, v2) or hi1 < lo2 or hi2 < lo1, (spans, seed)
        assert len(late) == len(set(late))


def test_reorder_leaves_heartbeats_in_place():
    import random
    keys = [(68, 1, 1), (68, 1, 0), (68, 1, 2), (68, 1, 3), (68, 1, 0), (68, 1, 4)]
    entries, keys = _entries(keys)
    out, moves = scenarios.reorder(entries, keys, scenarios.ReorderParams(1.0, 1),
                                   random.Random(0))
    # Beats (entries 1 and 4) never move and never count towards a depth.
    assert [e[2][0] for e in out] == [1, 2, 0, 4, 5, 3]
    assert [e[0] for e in out] == [1, 2, 2, 4, 5, 5]
    assert moves == [{"packet": [68, 1, 1], "after": [68, 1, 2], "depth": 1},
                     {"packet": [68, 1, 3], "after": [68, 1, 4], "depth": 1}]


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
    for name in ("loss", "gap", "reorder", "idle"):
        again = main.run_scenario(config(tmp_path), name)
        for attr in ("pcap_path", "intent_path", "book_key_path"):
            assert Path(getattr(again.run, attr)).read_bytes() == \
                Path(getattr(runs[name].run, attr)).read_bytes(), (name, attr)
        assert Path(again.feed_b_path).read_bytes() == Path(runs[name].feed_b_path).read_bytes()
        assert again.manifest == runs[name].manifest


def test_unknown_scenario_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown scenario"):
        main.run_scenario(config(tmp_path), "nope")
