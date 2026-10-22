"""Step 7c: edge orders, rare messages, schema evolution, malformed packets.

Everything is read from the pcaps with the independent decoder and the
fault-aware tests/wire.py; the manifest only says where things were put.
"""

import json
from pathlib import Path

import pytest

from b3gen import engine, main
from b3gen.flow import FlowInstrument, FlowParams, OrderFlow
from conftest import scenario_config
import sbe_decoder
from book_builder import BookBuilder
from replay import replay
from wire import read_packets, split_messages

DELETE = 51
NEWS, ORDER, MASS_DELETE, TRADE, EXEC_SUMMARY, EXEC_STATS, TRADE_BUST = 5, 50, 52, 53, 55, 56, 57
EOE = 0x80


def ids():
    return {i.name: i.security_id for i in main.default_config().instruments}


def intent(result):
    return [json.loads(line) for line in Path(result.run.intent_path).read_text().splitlines()]


def intent_at(result):
    return {(r["channel"], r["sequenceVersion"], r["sequenceNumber"], r["index"]): r
            for r in intent(result)}


def wire_at(result):
    """(channel, version, seq, index) -> (header, fields, message bytes, packet)."""
    out = {}
    for p in read_packets(result.run.pcap_path):
        for index, data in enumerate(p["messages"]):
            header, fields = sbe_decoder.decode(data)
            out[(p["channel"], p["version"], p["seq"], index)] = (header, fields, data, p)
    return out


def injections(result, kind):
    return [i for i in result.manifest["injections"] if i["kind"] == kind]


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["edge", "rare", "schema"])
def test_book_replay_matches_every_checkpoint(runs, name):
    result = runs[name]
    checks, rows = replay(result.run.pcap_path, result.run.book_key_path, ids())
    assert all(n == rows for n in checks.values()), (checks, rows)


def test_malformed_books_match_where_reliable(runs):
    result = runs["malformed"]
    unreliable = result.manifest["unreliable_books"]
    assert unreliable
    checks, rows = replay(result.run.pcap_path, result.run.book_key_path, ids(), unreliable)
    for name, n in checks.items():
        assert (0 < n < rows) if name in unreliable else n == rows, (name, n, rows)


# ---------------------------------------------------------------------------
# Edge orders
# ---------------------------------------------------------------------------

def test_supervisor_delete_thru_of_each_side(runs):
    result = runs["edge"]
    wire = wire_at(result)
    sides = []
    for inj in injections(result, "supervisor_delete_thru"):
        (where,) = inj["event_placements"]
        header, f, _, _ = wire[tuple(where)]
        assert header["templateId"] == MASS_DELETE and f["mDUpdateAction"] == 3  # DELETE_THRU
        assert f["matchEventIndicator"] == EOE and f["securityID"] == ids()[inj["instrument"]]
        sides.append(f["mDEntryType"])
    assert sorted(sides) == ["0", "1"]
    # The flow forgot the cancelled handles: nothing it sent was rejected.
    assert result.run.outcomes["SupervisorDeletedThru"] == 2
    assert result.run.outcomes["Rejected"] == 0


def test_firm_mass_cancel_is_one_event_across_instruments(runs):
    # A participant's mass cancel (Guidelines p.15, LAYOUTS.md 12.2): one
    # event of DeleteOrder_MBO for every resting order of the firm, across
    # several instruments of one channel, only the last with EndOfEvent.
    result = runs["edge"]
    (inj,) = injections(result, "firm_mass_cancel")
    places = [tuple(p) for p in inj["event_placements"]]
    wire = wire_at(result)
    msgs = [wire[p] for p in places]
    assert all(h["templateId"] == DELETE for h, _, _, _ in msgs)
    assert [f["matchEventIndicator"] for _, f, _, _ in msgs] == [0] * (len(msgs) - 1) + [EOE]
    assert len({f["securityID"] for _, f, _, _ in msgs}) >= 2
    assert {p[0] for p in places} == {inj["channel"]}
    assert len({f["transactTime"] for _, f, _, _ in msgs}) == 1
    channel = places[0][0]
    on_channel = sorted(k for k in wire if k[0] == channel)
    start = on_channel.index(places[0])
    assert on_channel[start:start + len(places)] == places  # nothing in between
    # Exactly the firm's resting orders when the event began, from the wire:
    # enteringFirm comes from each order's Order_MBO.
    builder, firm_of, held = BookBuilder(), {}, None
    sids = {ids()[n] for n in inj["instruments"]}
    for p in read_packets(result.run.pcap_path):
        def before(index, p=p):
            nonlocal held
            if (p["channel"], p["version"], p["seq"], index) == places[0]:
                held = {(sid, soid) for sid in sids for _, _, _, soid in builder.resting(sid)
                        if firm_of[(sid, soid)] == inj["firm"]}

        def after(index, p=p):
            header, f = sbe_decoder.decode(p["messages"][index])
            if header["templateId"] == ORDER:
                firm_of[(f["securityID"], f["secondaryOrderID"])] = f["enteringFirm"]
        builder.apply_packet(p["channel"], p["version"], p["seq"], p["messages"],
                             before=before, after=after)
    assert held == {(f["securityID"], f["secondaryOrderID"]) for _, f, _, _ in msgs}
    assert sum(inj["orders_cancelled"].values()) == len(msgs)
    assert result.run.outcomes["FirmMassCancelled"] == 1 and result.run.outcomes["Rejected"] == 0


def test_delete_thru_handles_leave_the_flow():
    inst = FlowInstrument("X", tick=100, lot=100, low_limit=1_000, high_limit=1_000_000,
                          reference_price=50_000)
    flow = OrderFlow([inst], FlowParams(rates={"X": (10, 100, 1000)}), seed=3, start_ns=0)
    live = flow.flows["X"].live
    for handle, side in ((1, engine.BUY), (2, engine.BUY), (3, engine.SELL)):
        flow.on_outcome(engine.Rested("X", 0, handle, handle, side, 50_000, 100, 1, 0))
    views = tuple(engine.OrderView(h, h, engine.BUY, 50_000, 100, 1, 0) for h in (1, 2))
    flow.on_outcome(engine.SupervisorDeletedThru("X", 1, engine.BUY, views))
    assert set(live.orders) == {3}


def test_sweep_event_spans_three_packets(runs):
    result = runs["edge"]
    (inj,) = injections(result, "sweep")
    places = [tuple(p) for p in inj["event_placements"]]
    wire = wire_at(result)
    messages = [wire[p] for p in places]
    assert messages[0][0]["templateId"] == EXEC_SUMMARY
    assert messages[-1][1]["matchEventIndicator"] == EOE
    assert all(not m[1].get("matchEventIndicator", 0) & EOE for m in messages[:-1])
    assert sum(m[0]["templateId"] == TRADE for m in messages) == inj["orders_hit"] == 30
    packets = sorted({p[:3] for p in places})
    assert len(packets) >= 3
    # Contiguous on the channel: nothing else in between.
    channel = places[0][0]
    on_channel = sorted(k for k in wire if k[0] == channel)
    start = on_channel.index(places[0])
    assert on_channel[start:start + len(places)] == places


# ---------------------------------------------------------------------------
# Rare messages
# ---------------------------------------------------------------------------

def test_trade_bust_copies_a_real_trade(runs):
    result = runs["rare"]
    (inj,) = injections(result, "trade_bust")
    wire = wire_at(result)
    _, trade, _, _ = wire[tuple(inj["trade_placements"][0])]
    header, bust, _, _ = wire[tuple(inj["event_placements"][0])]
    assert header["templateId"] == TRADE_BUST and bust["matchEventIndicator"] == EOE
    for f in ("securityID", "tradingSessionID", "mDEntryPx", "mDEntrySize", "tradeID",
              "tradeDate"):
        assert bust[f] == trade[f], f
    assert bust["transactTime"] >= trade["transactTime"]


def test_trade_bust_leaves_statistics_alone(runs):
    # Inference: the next ExecutionStatistics still counts the busted trade.
    sid = ids()["WIN"]
    trades = volume = 0
    seen_bust = after_bust = False
    for p in read_packets(runs["rare"].run.pcap_path):
        for data in p["messages"]:
            h, f = sbe_decoder.decode(data)
            if f is None or f.get("securityID") != sid:
                continue
            if h["templateId"] == TRADE:
                trades += 1
                volume += f["mDEntrySize"]
            elif h["templateId"] == TRADE_BUST:
                seen_bust = True
            elif h["templateId"] == EXEC_STATS:
                assert (f["numberOfTrades"], f["tradeVolume"]) == (trades, volume)
                after_bust |= seen_bust
    assert after_bust


def news_from_wire(result):
    """Every News reassembled the way a client must, since newsID is null on
    trading channels: per channel in sequence order, partNumber 1 opens a
    News and partNumber == partCount closes it. Returns {placement of the
    last part: [(placement, header, fields)]}."""
    out, open_news = {}, {}
    for key, (header, fields, _, _) in sorted(wire_at(result).items()):
        if header["templateId"] != NEWS:
            continue
        parts = open_news.setdefault(key[0], [])
        assert fields["partNumber"] == len(parts) + 1, (key, fields["partNumber"], len(parts))
        parts.append((key, header, fields))
        if fields["partNumber"] == fields["partCount"]:
            out[key] = open_news.pop(key[0])
    assert not open_news, "a News never got its last part"
    return out


@pytest.mark.parametrize("name", ["rare", "schema"])
def test_news_reassembly_matches_intent(runs, name):
    result = runs[name]
    by_last = news_from_wire(result)
    rows = intent_at(result)
    expected = {k: r["news"] for k, r in rows.items() if "news" in r}
    assert set(by_last) == set(expected) == \
        {tuple(i["part_placements"][-1]) for i in injections(result, "news")}
    for last, parts in by_last.items():
        fields = [f for _, _, f in parts]
        count = fields[0]["partCount"]
        assert [f["partNumber"] for f in fields] == list(range(1, count + 1))
        assert all(f["partCount"] == count for f in fields)
        # Trading-channel News: newsID null (Unified News Channel only), "en".
        assert all(f["newsID"] == 0 and f["languageCode"] == "en" for f in fields)
        assert all("newsID" in rows[k]["null"] for k, _, _ in parts)
        text = b"".join(f["text"] for f in fields)
        assert all(f["totalTextLength"] == len(text) for f in fields)
        # Headline in part 1 only (inference).
        assert fields[0]["headline"] and all(f["headline"] == b"" for f in fields[1:])
        assert all(f["matchEventIndicator"] == EOE for f in fields)
        assert (fields[0]["headline"].decode(), text.decode()) == \
            (expected[last]["headline"], expected[last]["text"])


def test_multi_part_news_interleaved_and_split(runs):
    result = runs["rare"]
    (multi,) = [i for i in injections(result, "news") if i["part_count"] > 1]
    single = [i for i in injections(result, "news") if i["part_count"] == 1]
    assert multi["part_count"] >= 3 and multi["instrument"] is None and multi["channel"] == 70
    wire = wire_at(result)
    places = [tuple(p) for p in multi["part_placements"]]
    assert all(wire[p][1]["securityID"] == 0 for p in places)          # market-wide: null
    assert all(wire[tuple(p)][1]["securityID"] == ids()["WIN"]
               for s in single for p in s["part_placements"])
    assert len({p[:3] for p in places}) == len(places)                  # separate packets
    on_channel = sorted(k for k in wire if k[0] == 70)
    for a, b in zip(places, places[1:]):
        between = on_channel[on_channel.index(a) + 1:on_channel.index(b)]
        assert any(wire[k][0]["templateId"] != NEWS for k in between)   # live traffic


# ---------------------------------------------------------------------------
# Schema evolution
# ---------------------------------------------------------------------------

def test_extended_messages_decode_known_fields_and_skip_the_rest(runs):
    result = runs["schema"]
    wire = wire_at(result)
    rows = intent_at(result)
    places = [tuple(p) for p in injections(result, "extend")[0]["event_placements"]]
    places += [tuple(p) for i in injections(result, "news") for p in i["part_placements"]]
    assert len(places) == 4
    for p in places:
        header, fields, data, _ = wire[p]
        schema_block = sbe_decoder.LAYOUTS[header["templateId"]][0]
        assert header["blockLength"] == schema_block + 8 and header["version"] == 18
        assert data[12 + schema_block:12 + schema_block + 8] == bytes(8)
        row = rows[p]
        assert (row["blockLength"], row["version"]) == (header["blockLength"], 18)
        for name, value in row["fields"].items():
            got = fields[name]
            assert (got.decode() if isinstance(got, bytes) else got) == value, name
    templates = {wire[p][0]["templateId"] for p in places}
    assert templates == {ORDER, NEWS}


def test_older_version_messages_lack_the_later_fields(runs):
    # LAYOUTS.md section 5: fields whose sinceVersion is above the header's
    # version are not there. Order_MBO v15: no mDEntryPrevSize (since v16),
    # blockLength 64; v14: no transactTime either (since v15), blockLength 56.
    result = runs["schema"]
    wire = wire_at(result)
    rows = intent_at(result)
    (inj,) = injections(result, "older_version")
    places = [tuple(p) for p in inj["event_placements"]]
    want = {15: (64, ["mDEntryPrevSize"]), 14: (56, ["transactTime", "mDEntryPrevSize"])}
    assert inj["versions"] == [15, 15, 14, 14] and len(places) == 4
    for p, version in zip(places, inj["versions"]):
        header, fields, data, _ = wire[p]
        block, missing = want[version]
        assert header["templateId"] == ORDER and fields["mDUpdateAction"] == 0
        assert (header["version"], header["blockLength"], header["messageLength"]) == \
            (version, block, 12 + block) == (version, block, len(data))
        assert not set(missing) & set(fields)
        row = rows[p]
        assert (row["version"], row["blockLength"], row["absent"]) == (version, block, missing)
        assert not set(missing) & (set(row["fields"]) | set(row["null"]))
        assert fields == row["fields"]


def test_unknown_template_is_skipped_by_message_length(runs):
    result = runs["schema"]
    (inj,) = injections(result, "unknown")
    (place,) = [tuple(p) for p in inj["message_placements"]]
    wire = wire_at(result)
    header, fields, data, _ = wire[place]
    assert header["templateId"] == 999 and fields is None
    assert intent_at(result)[place]["fields"]["body"] == data[12:].hex()
    assert place[3] > 0  # a real message before it in the same packet ...
    after = wire[place[:3] + (place[3] + 1,)]  # ... and one after it, decoded normally
    assert after[1] is not None


def test_every_other_intent_row_has_schema_header_values(runs):
    rows = intent(runs["schema"])
    odd = [r for r in rows if r["version"] != 17]
    assert len(odd) == 9  # 3 extended Order_MBO + 1 News + 1 unknown + 4 older Order_MBO
    for r in rows:
        if r["version"] == 17:
            assert r["blockLength"] == sbe_decoder.LAYOUTS[r["templateId"]][0]


# ---------------------------------------------------------------------------
# Malformed
# ---------------------------------------------------------------------------

def test_every_fault_is_found_where_the_manifest_says(runs):
    result = runs["malformed"]
    expected = result.manifest["faults"]
    assert sorted(f["fault"] for f in expected) == sorted(
        ["short", "length_past_end", "length_zero", "length_too_small", "bad_encoding",
         "truncated"])
    found = []
    previous = None
    for p in read_packets(result.run.pcap_path):
        if p["fault"] is not None:
            kind, offset = p["fault"]
            if kind == "short":
                found.append({"fault": "short", "channel": previous[0], "after": list(previous),
                              "length": len(p["payload"])})
            else:
                found.append({"fault": kind, "channel": p["channel"],
                              "sequenceVersion": p["version"], "sequenceNumber": p["seq"],
                              "offset": offset, "messages_before": len(p["messages"])})
        if p["seq"]:
            previous = (p["channel"], p["version"], p["seq"])
    key = lambda f: json.dumps(f, sort_keys=True)
    assert sorted(found, key=key) == sorted(expected, key=key)


def test_sequencing_continues_after_faults(runs):
    # Only the stray datagram lacks a header; every sequence number is there once.
    by_channel = {}
    for p in read_packets(runs["malformed"].run.pcap_path):
        if p["seq"]:
            by_channel.setdefault(p["channel"], []).append(p["seq"])
    for seqs in by_channel.values():
        assert seqs == list(range(1, len(seqs) + 1))


def test_unreliable_books_are_the_instruments_with_discarded_book_messages(runs):
    result = runs["malformed"]
    rows = intent_at(result)
    want = {}
    for fault in result.manifest["faults"]:
        if fault["fault"] == "short":
            continue
        packet = (fault["channel"], fault["sequenceVersion"], fault["sequenceNumber"])
        for (c, v, s, i), row in sorted(rows.items()):
            if (c, v, s) == packet and i >= fault["messages_before"] and \
                    row["templateId"] not in (2, NEWS) and row["instrument"]:
                want.setdefault(row["instrument"], list(packet))
    assert result.manifest["unreliable_books"] == want


def test_split_messages_never_loops():
    payload = bytes(16) + bytes(12)  # a message header with messageLength 0
    assert split_messages(payload) == ([], ("length_zero", 16))
    assert split_messages(bytes(10)) == ([], ("short", 0))
    assert split_messages(bytes(16) + b"\x05\x00" + bytes(3)) == ([], ("truncated", 16))


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_injection_scenarios_are_deterministic(tmp_path, runs):
    for name in ("edge", "rare", "schema", "malformed"):
        again = main.run_scenario(scenario_config(tmp_path), name)
        for attr in ("pcap_path", "intent_path", "book_key_path"):
            assert Path(getattr(again.run, attr)).read_bytes() == \
                Path(getattr(runs[name].run, attr)).read_bytes(), (name, attr)
        assert Path(again.feed_b_path).read_bytes() == Path(runs[name].feed_b_path).read_bytes()
        assert again.manifest == runs[name].manifest
