"""Step 7b: restart and reset scenarios (spec/LAYOUTS.md section 12).

Feed A is decoded independently and replayed by tests/book_builder.py with
the client-side rules (sequencing, SequenceReset, ChannelReset, EmptyBook,
rptSeq). Where each reset happened comes from the manifest; everything else
is checked from the wire.
"""

import json
from pathlib import Path

import pytest

from b3gen import main, scenarios
from b3gen.packet import Packetizer
from book_builder import BookBuilder
from conftest import scenario_config
import sbe_decoder
from wire import read_packets, top5

RESETS = scenarios.RESET_KINDS
WITH_REBUILD = ("failover", "channel_reset", "instrument_reset", "weekly_start")
WITH_SEQUENCE_RESET = ("failover", "weekly_start")
NEW_SESSION = ("session_change", "weekly_start")
SEQUENCE_RESET, SEQUENCE, EMPTY_BOOK, GROUP_PHASE, CHANNEL_RESET = 1, 2, 9, 10, 11
ORDER, TRADE, EXEC_STATS = 50, 53, 56
RECOVERY, EOE = 0x20, 0x80
TRADING_SESSION_CHANGE = 4


def reset_of(result):
    (reset,) = result.manifest["resets"]
    return reset


def book_key(result):
    return [json.loads(line) for line in Path(result.run.book_key_path).read_text().splitlines()]


def messages_on(packets, channel):
    """[((channel, version, seq, index), header, fields)] on one channel, wire order."""
    out = []
    for p in packets:
        if p["channel"] == channel and p["seq"]:
            for index, data in enumerate(p["messages"]):
                header, fields = sbe_decoder.decode(data)
                out.append(((channel, p["version"], p["seq"], index), header, fields))
    return out


def ids():
    return {i.name: i.security_id for i in main.default_config().instruments}


def channel_of():
    return {i.name: i.channel for i in main.default_config().instruments}


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", RESETS)
def test_book_replay_with_client_rules_matches_every_checkpoint(runs, name):
    result = runs[name]
    rows = book_key(result)
    by_id = ids()
    expect = {}
    for row in rows:
        for inst, book in row["books"].items():
            expect.setdefault(tuple(book["as_of"]), []).append((inst, book))
    builder = BookBuilder()
    checked = 0
    for p in read_packets(result.run.pcap_path):
        def check(index, p=p):
            nonlocal checked
            for inst, book in expect.pop((p["channel"], p["version"], p["seq"], index), []):
                assert top5(builder, by_id[inst]) == (book["bids"], book["offers"]), (inst, p["seq"])
                checked += 1
        builder.apply_packet(p["channel"], p["version"], p["seq"], p["messages"], after=check)
    assert not expect
    assert checked == len(rows) * len(by_id)
    # A checkpoint right after every rebuild (and so it was checked above).
    keys = {tuple(row["key"]) for row in rows}
    for inst, (_, last) in reset_of(result)["rebuilds"].items():
        assert tuple(last) in keys, inst


@pytest.mark.parametrize("name", WITH_REBUILD)
def test_rebuild_republishes_the_book_it_replaces(runs, name):
    # Each rebuild must carry exactly the orders the client held when the
    # reset began on that channel, in priority order, with their original
    # soid and mDInsertTimestamp, and transactTime = the rebuild time.
    result = runs[name]
    reset = reset_of(result)
    rebuild_time = reset.get("session_start_ns", reset["time_ns"])
    by_id = ids()
    on_channel = {}
    for inst, channel in channel_of().items():
        on_channel.setdefault(channel, []).append(inst)
    # Where the reset starts on each channel: its SequenceReset, else the
    # reset's first message (ChannelReset or EmptyBook).
    snap_at = {tuple(key): int(c) for c, key in reset["sequence_resets"].items()} or \
        {tuple(reset["first"]): reset["first"][0]}
    builder = BookBuilder()
    insert_time = {}  # soids are per segment, so keyed by instrument too
    held = {}
    packets = read_packets(result.run.pcap_path)
    for p in packets:
        def before(index, p=p):
            channel = snap_at.get((p["channel"], p["version"], p["seq"], index))
            if channel is not None:
                for inst in on_channel[channel]:
                    sid = by_id[inst]
                    held[inst] = [(side, price, qty, soid, insert_time[(sid, soid)])
                                  for side, price, qty, soid in builder.resting(sid)]

        def after(index, p=p):
            header, fields = sbe_decoder.decode(p["messages"][index])
            if header["templateId"] == ORDER:
                insert_time[(fields["securityID"], fields["secondaryOrderID"])] = \
                    fields["mDInsertTimestamp"]
        builder.apply_packet(p["channel"], p["version"], p["seq"], p["messages"],
                             after=after, before=before)
    assert reset["rebuilds"]
    with_book = {inst for inst, orders in held.items() if orders}
    if name == "instrument_reset":
        with_book &= {reset["target"]}
    assert set(reset["rebuilds"]) == with_book
    for inst, (first, last) in reset["rebuilds"].items():
        msgs = messages_on(packets, first[0])
        keys = [m[0] for m in msgs]
        orders = msgs[keys.index(tuple(first)) + 1:keys.index(tuple(last)) + 1]
        got = [({"0": "BUY", "1": "SELL"}[f["mDEntryType"]], f["mDEntryPx"],
                f["mDEntrySize"], f["secondaryOrderID"], f["mDInsertTimestamp"])
               for _, _, f in orders]
        assert got == held[inst], inst
        assert all(h["templateId"] == ORDER and f["mDUpdateAction"] == 0 and
                   f["transactTime"] == rebuild_time for _, h, f in orders)


@pytest.mark.parametrize("name", WITH_REBUILD)
def test_rebuild_match_event_indicators_and_rptseq(runs, name):
    result = runs[name]
    reset = reset_of(result)
    rebuild_time = reset.get("session_start_ns", reset["time_ns"])
    by_id = ids()
    packets = read_packets(result.run.pcap_path)
    for inst, (first, last) in reset["rebuilds"].items():
        msgs = messages_on(packets, first[0])
        keys = [m[0] for m in msgs]
        rebuild = msgs[keys.index(tuple(first)):keys.index(tuple(last)) + 1]
        (_, h, f), orders = rebuild[0], rebuild[1:]
        assert h["templateId"] == EMPTY_BOOK and f["securityID"] == by_id[inst]
        assert f["matchEventIndicator"] == RECOVERY and f["mDEntryTimestamp"] == rebuild_time
        assert orders, inst
        assert [f["matchEventIndicator"] for _, _, f in orders] == \
            [RECOVERY] * (len(orders) - 1) + [RECOVERY | EOE]
        # No live message for the instrument inside its own rebuild.
        assert all(h["templateId"] == ORDER and f["securityID"] == by_id[inst]
                   for _, h, f in orders)
        assert [f["rptSeq"] for _, _, f in orders] == list(range(1, len(orders) + 1))
        # After the rebuild, live updates go on from there without RecoveryMsg.
        after = [(h, f) for _, h, f in msgs[keys.index(tuple(last)) + 1:]
                 if f.get("securityID") == by_id[inst]]
        assert after and after[0][1].get("rptSeq") == len(orders) + 1
        assert all(not f.get("matchEventIndicator", 0) & RECOVERY for _, f in after)


@pytest.mark.parametrize("name", WITH_REBUILD)
def test_rptseq_restarts_only_for_rebuilt_instruments(runs, name):
    reset = reset_of(runs[name])
    by_id = ids()
    rebuilt = {by_id[i] for i in reset["rebuilds"]}
    empty_books = set()
    restarts = set()
    last = {}
    for p in read_packets(runs[name].run.pcap_path):
        for data in p["messages"]:
            h, f = sbe_decoder.decode(data)
            if h["templateId"] == EMPTY_BOOK:
                empty_books.add(f["securityID"])
            elif "rptSeq" in f and "securityID" in f:
                sid = f["securityID"]
                if sid in last and f["rptSeq"] != last[sid] + 1:
                    assert f["rptSeq"] == 1, sid
                    restarts.add(sid)
                last[sid] = f["rptSeq"]
    assert empty_books == restarts == rebuilt
    if name == "instrument_reset":
        assert rebuilt == {by_id["WIN"]}


@pytest.mark.parametrize("name", ("channel_reset", "weekly_start"))
def test_channel_reset_is_its_own_recovery_event(runs, name):
    reset = reset_of(runs[name])
    channels = [reset["target"]] if name == "channel_reset" else [68, 70]
    packets = read_packets(runs[name].run.pcap_path)
    for channel in channels:
        msgs = messages_on(packets, channel)
        resets = [i for i, (_, h, _) in enumerate(msgs) if h["templateId"] == CHANNEL_RESET]
        assert len(resets) == 1
        i = resets[0]
        assert msgs[i][2]["matchEventIndicator"] == RECOVERY | EOE
        previous = [m for m in msgs[:i] if m[1]["templateId"] != SEQUENCE_RESET][-1]
        assert previous[2]["matchEventIndicator"] & EOE  # previous event closed
        assert msgs[i + 1][1]["templateId"] == EMPTY_BOOK  # rebuilds follow at once
    if name == "channel_reset":
        other = messages_on(packets, 70)
        assert not any(h["templateId"] in (CHANNEL_RESET, EMPTY_BOOK) for _, h, _ in other)


@pytest.mark.parametrize("name", WITH_REBUILD)
def test_no_live_message_on_the_channel_until_every_rebuild_is_done(runs, name):
    # v1: all instruments rebuilt, then live flow resumes (open item J).
    reset = reset_of(runs[name])
    packets = read_packets(runs[name].run.pcap_path)
    for channel in {first[0] for first, _ in reset["rebuilds"].values()}:
        msgs = messages_on(packets, channel)
        keys = [m[0] for m in msgs]
        spans = [(keys.index(tuple(f)), keys.index(tuple(l)))
                 for f, l in reset["rebuilds"].values() if f[0] == channel]
        lo, hi = min(s for s, _ in spans), max(e for _, e in spans)
        assert all(m[2].get("matchEventIndicator", 0) & RECOVERY for m in msgs[lo:hi + 1])


# ---------------------------------------------------------------------------
# Sequencing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", WITH_SEQUENCE_RESET)
def test_sequence_reset_alone_at_x_plus_1_then_version_up(runs, name):
    reset = reset_of(runs[name])
    packets = read_packets(runs[name].run.pcap_path)
    assert set(reset["sequence_resets"]) == {"68", "70"}
    for channel, (ch, version, seq, index) in reset["sequence_resets"].items():
        real = [p for p in packets if p["channel"] == int(channel) and p["seq"]]
        i = next(k for k, p in enumerate(real) if (p["version"], p["seq"]) == (version, seq))
        p = real[i]
        assert index == 0 and len(p["messages"]) == 1
        h, f = sbe_decoder.decode(p["messages"][0])
        assert h["templateId"] == SEQUENCE_RESET and h["blockLength"] == 0 and f == {}
        assert (real[i - 1]["version"], real[i - 1]["seq"]) == (version, seq - 1)
        assert (real[i + 1]["version"], real[i + 1]["seq"]) == (version + 1, 1)
        assert all(q["version"] == version for q in real[:i + 1])
        assert all(q["version"] == version + 1 for q in real[i + 1:])
    no_reset = runs["channel_reset"].run.pcap_path
    assert {p["version"] for p in read_packets(no_reset)} == {1}


@pytest.mark.parametrize("name", NEW_SESSION)
def test_start_of_day_heartbeats(runs, name):
    # Stream down overnight / over the weekend: no heartbeats until 3
    # start-of-day ones; their nextSeqNo is the next real packet, which in
    # weekly_start is the SequenceReset packet.
    result = runs[name]
    reset = reset_of(result)
    packets = read_packets(result.run.pcap_path)
    assert result.run.heartbeats == 6
    for channel in (68, 70):
        beats = [p for p in packets if p["channel"] == channel and not p["seq"]]
        assert len(beats) == 3
        start = reset["session_start_ns"]
        assert [b["sending"] for b in beats] == \
            [start - main.START_OF_DAY_NS + k * 1_000_000_000 for k in range(3)]
        after = next(p for p in packets if p["channel"] == channel and p["seq"]
                     and p["sending"] > beats[-1]["sending"])
        for b in beats:
            assert sbe_decoder.decode(b["messages"][0])[1]["nextSeqNo"] == after["seq"]
        if name == "weekly_start":
            assert [after["version"], after["seq"]] == reset["sequence_resets"][str(channel)][1:3]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def stats_stream(result):
    """Per securityID: [("trade", tradeID, size, tradeDate) or
    ("stats", numberOfTrades, tradeVolume, tradeDate) or ("session",)] in wire order."""
    out = {sid: [] for sid in ids().values()}
    for p in read_packets(result.run.pcap_path):
        for data in p["messages"]:
            h, f = sbe_decoder.decode(data)
            if h["templateId"] == TRADE:
                out.setdefault(f["securityID"], []).append(
                    ("trade", f["tradeID"], f["mDEntrySize"], f["tradeDate"]))
            elif h["templateId"] == EXEC_STATS:
                out.setdefault(f["securityID"], []).append(
                    ("stats", f["numberOfTrades"], f["tradeVolume"], f["tradeDate"]))
            elif h["templateId"] == GROUP_PHASE and f["securityTradingEvent"] == TRADING_SESSION_CHANGE:
                for stream in out.values():  # one per group: count a session once
                    if not stream or stream[-1] != ("session",):
                        stream.append(("session",))
    return out


def check_sessions(stream):
    """Trade IDs, counts and volumes run on within a session and restart at
    each TRADING_SESSION_CHANGE. Returns the tradeDates per session."""
    dates = [set()]
    trades = volume = 0
    expected_id = 1
    for item in stream:
        if item[0] == "session":
            dates.append(set())
            trades = volume = 0
            expected_id = 1
        elif item[0] == "trade":
            assert item[1] == expected_id
            expected_id += 1
            trades += 1
            volume += item[2]
            dates[-1].add(item[3])
        else:
            assert item[1:3] == (trades, volume)
            dates[-1].add(item[3])
    return dates


@pytest.mark.parametrize("name", ("failover", "channel_reset", "instrument_reset"))
def test_statistics_survive_resets(runs, name):
    for sid, stream in stats_stream(runs[name]).items():
        # Only the file's own session start: one session, one tradeDate.
        assert sum(1 for x in stream if x[0] == "session") == 1
        dates = check_sessions(stream)
        assert len(dates) == 2 and not dates[0] and len(dates[1]) == 1
    # The reset really happened between trades of the same session.
    assert reset_of(runs[name])["rebuilds"]


@pytest.mark.parametrize("name", NEW_SESSION)
def test_session_change_resets_statistics_trade_id_and_trade_date(runs, name):
    reset = reset_of(runs[name])
    streams = stats_stream(runs[name])
    traded_after = 0
    for sid, stream in streams.items():
        dates = check_sessions(stream)
        # The file's own session start sends one TRADING_SESSION_CHANGE
        # before any trade, so sessions are: (empty), before, after.
        assert len(dates) == 3 and not dates[0]
        assert dates[1] == {reset["trade_date_before"]}
        assert dates[2] in (set(), {reset["trade_date_after"]})
        traded_after += bool(dates[2])
    assert traded_after >= 3


def test_weekly_start_order(runs):
    # SequenceReset, ChannelReset, rebuilds, then TRADING_SESSION_CHANGE and
    # the Monday session values, then live flow.
    reset = reset_of(runs["weekly_start"])
    packets = read_packets(runs["weekly_start"].run.pcap_path)
    for channel in (68, 70):
        msgs = [m for m in messages_on(packets, channel)
                if m[0][1] == reset["sequence_resets"][str(channel)][1] + 1]
        templates = [h["templateId"] for _, h, _ in msgs]
        phase = templates.index(GROUP_PHASE)
        assert templates[0] == CHANNEL_RESET
        assert set(templates[1:phase]) == {EMPTY_BOOK, ORDER}
        assert msgs[phase][2]["securityTradingEvent"] == TRADING_SESSION_CHANGE
        assert msgs[phase][2]["tradeDate"] == reset["trade_date_after"]


# ---------------------------------------------------------------------------
# Units and determinism
# ---------------------------------------------------------------------------

def test_bump_version_sends_the_final_message_alone():
    p = Packetizer([68], 0, window_ns=5000)
    msg = b"\x0c\x00" + bytes(10)
    p.add(0, 68, msg, tag="a")
    p.add(100, 68, msg, tag="b")
    p.bump_version(200, 68, final_message=b"\x0c\x00\x00\x00" + bytes(8), final_tag="reset")
    p.add(300, 68, msg, tag="c")
    out = p.finish(10_000)
    where = {tag: (pl.sequence_version, pl.sequence_number, pl.index)
             for tag, pl in out.placements.items()}
    assert where == {"a": (1, 1, 0), "b": (1, 1, 1), "reset": (1, 2, 0), "c": (2, 1, 0)}
    assert [len(data) for _, _, data in out.packets] == [16 + 24, 16 + 12, 16 + 12]


def test_stream_down_has_no_heartbeats_until_it_comes_up():
    p = Packetizer([68], 0, window_ns=5000)
    msg = b"\x0c\x00" + bytes(10)
    p.add(0, 68, msg)
    p.stream_down(1_000, 68, up_ns=100_000_000_000)
    p.add(102_500_000_000, 68, msg)
    out = p.finish(102_500_000_000)
    assert [hb.placement.sending_time for hb in out.heartbeats] == \
        [100_000_000_000, 101_000_000_000, 102_000_000_000]
    assert all(hb.next_seq_no == 2 for hb in out.heartbeats)


def test_unknown_reset_kind_raises():
    with pytest.raises(ValueError, match="unknown reset"):
        scenarios.Reset("reboot", 10)


def test_reset_scenarios_are_deterministic(tmp_path, runs):
    for name in RESETS:
        again = main.run_scenario(scenario_config(tmp_path), name)
        for attr in ("pcap_path", "intent_path", "book_key_path"):
            assert Path(getattr(again.run, attr)).read_bytes() == \
                Path(getattr(runs[name].run, attr)).read_bytes(), (name, attr)
        assert again.manifest == runs[name].manifest


@pytest.fixture(scope="module")
def tight_session_change(tmp_path_factory):
    # A 0.2% band, so the flow presses on it and a stale band would show.
    return main.run_scenario(scenario_config(tmp_path_factory.mktemp("tight"), band_bps=20),
                             "session_change")


@pytest.mark.parametrize("name", NEW_SESSION + ("tight",))
def test_new_session_bands_follow_the_close_and_bind_new_orders(runs, tight_session_change,
                                                                  name):
    # Each session's PriceBand is +-band_bps of the previous close (the
    # ClosingPrice DAILY just before it, 8 decimals), and every new live
    # order after it stays inside it.
    result = tight_session_change if name == "tight" else runs[name]
    bps = 20 if name == "tight" else main.RunConfig.band_bps
    at_edge = 0
    band = {}
    last_close = {}
    new_bands = 0
    checked = 0
    for p in read_packets(result.run.pcap_path):
        for data in p["messages"]:
            h, f = sbe_decoder.decode(data)
            sid = f.get("securityID")
            if h["templateId"] == 17 and f["openCloseSettlFlag"] == 0:  # DAILY
                last_close[sid] = f["mDEntryPx"] // 10_000
            elif h["templateId"] == 22:
                band[sid] = (f["lowLimitPrice"], f["highLimitPrice"])
                if sid in last_close:
                    close = last_close[sid]
                    assert band[sid] == (close * (10_000 - bps) // 10_000,
                                         close * (10_000 + bps) // 10_000), sid
                    new_bands += 1
            elif h["templateId"] == ORDER and f["mDUpdateAction"] == 0 \
                    and not f["matchEventIndicator"] & RECOVERY:
                low, high = band[sid]
                assert low <= f["mDEntryPx"] <= high, (sid, f["mDEntryPx"], band[sid])
                checked += 1
                at_edge += high - f["mDEntryPx"] < 0.2 * (high - low) or \
                    f["mDEntryPx"] - low < 0.2 * (high - low)
    assert new_bands == 4 and checked > 1000
    if name == "tight":
        assert at_edge > 100  # the band really binds
