"""End-to-end tests: order flow -> engine -> publisher -> packetizer -> pcap.

The pcap is decoded independently (tests/pcap_reader.py + tests/sbe_decoder.py,
neither imports b3gen) and must match the intent log row by row; a book
rebuilt from the pcap (tests/book_builder.py) must match the book key at every
checkpoint.
"""

import json
import struct
import time
from pathlib import Path

import pytest

from b3gen import main
from b3gen.flow import BUSY, CALM, FlowParams
from book_builder import BookBuilder
from layouts_md import MESSAGE_LAYOUTS
import pcap_reader
import sbe_decoder


@pytest.fixture(scope="module")
def big_run(tmp_path_factory):
    """One ~20k-message run with per-arrival records, shared by the tests."""
    config = main.default_config(seed=1, target_messages=20_000,
                                 out_dir=tmp_path_factory.mktemp("big"), record=True)
    start = time.perf_counter()
    result = main.run(config)
    return config, result, time.perf_counter() - start


@pytest.fixture(scope="module")
def sparse_run(tmp_path_factory):
    """Low arrival rates, so channels go idle and heartbeats appear."""
    base = main.default_config(seed=4, target_messages=600,
                               out_dir=tmp_path_factory.mktemp("sparse"), book_key_every=25)
    flow = FlowParams(rates={name: (0.4, 4.0) for name in base.flow.rates},
                      calm_mean_ns=8_000_000_000, busy_mean_ns=2_000_000_000)
    config = main.default_config(seed=4, target_messages=600, out_dir=base.out_dir,
                                 book_key_every=25, flow=flow)
    return config, main.run(config)


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]


def decode_wire(pcap_path):
    """Every message in the pcap, decoded independently, in wire order."""
    _, records = pcap_reader.read_pcap(Path(pcap_path).read_bytes())
    out = []
    for rec in records:
        payload = rec["payload"]
        channel, _, version, seq, sending = struct.unpack_from("<BBHIQ", payload, 0)
        pos, index = 16, 0
        while pos < len(payload):
            (length,) = struct.unpack_from("<H", payload, pos)
            data = payload[pos:pos + length]
            out.append(((channel, version, seq, index, sending), data))
            pos += length
            index += 1
        assert pos == len(payload)
    return out


def security_map(config):
    return {i.security_id: i.name for i in config.instruments}


def independent_row(placement, data, securities):
    channel, version, seq, index, sending = placement
    header, fields = sbe_decoder.decode(data)
    template = header["templateId"]
    optional = MESSAGE_LAYOUTS[template]["optional_nulls"]
    nulls = [name for name in fields if name in optional and
             (fields[name] == optional[name] or (isinstance(fields[name], str) and fields[name] == ""))]
    return {"channel": channel, "sequenceVersion": version, "sequenceNumber": seq,
            "index": index, "sendingTime": sending, "templateId": template,
            "instrument": securities.get(fields.get("securityID")),
            "fields": fields, "null": nulls}


def top5(builder, security_id):
    levels = {"BUY": [], "SELL": []}
    for side, price, qty, _ in builder.resting(security_id):
        side_levels = levels[side]
        if side_levels and side_levels[-1][0] == price:
            side_levels[-1][1] += qty
            side_levels[-1][2] += 1
        else:
            side_levels.append([price, qty, 1])
    return levels["BUY"][:5], levels["SELL"][:5]


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("which", ["big_run", "sparse_run"])
def test_pcap_decodes_to_the_intent_log(which, request):
    config, result = request.getfixturevalue(which)[:2]
    wire = decode_wire(result.pcap_path)
    intent = read_jsonl(result.intent_path)
    assert len(wire) == len(intent) == result.messages + result.heartbeats
    securities = security_map(config)
    for (placement, data), row in zip(wire, intent):
        assert independent_row(placement, data, securities) == row


def test_sparse_run_has_heartbeats(sparse_run):
    _, result = sparse_run
    rows = [r for r in read_jsonl(result.intent_path) if r["templateId"] == 2]
    assert len(rows) == result.heartbeats > 0
    assert all(r["sequenceNumber"] == 0 and r["index"] == 0 for r in rows)


@pytest.mark.parametrize("which", ["big_run", "sparse_run"])
def test_book_rebuilt_from_pcap_matches_book_key(which, request):
    config, result = request.getfixturevalue(which)[:2]
    key_rows = read_jsonl(result.book_key_path)
    assert key_rows, "no checkpoints"
    by_id = {i.name: i.security_id for i in config.instruments}
    expect = {}
    for row in key_rows:
        assert row["books"].keys() == by_id.keys()
        for name, book in row["books"].items():
            expect.setdefault(tuple(book["as_of"]), []).append((name, book))
    builder = BookBuilder()
    checked = 0
    for (channel, version, seq, index, _), data in decode_wire(result.pcap_path):
        builder.apply(data)
        for name, book in expect.pop((channel, version, seq, index), []):
            assert top5(builder, by_id[name]) == (book["bids"], book["offers"]), name
            checked += 1
    assert not expect, "as_of placements never reached on the wire"
    assert checked == len(key_rows) * len(by_id)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def outputs(result):
    return [Path(p).read_bytes() for p in (result.pcap_path, result.intent_path,
                                           result.book_key_path)]


def test_same_seed_gives_identical_files(tmp_path):
    a = main.run(main.default_config(seed=7, target_messages=4000, out_dir=tmp_path / "a"))
    b = main.run(main.default_config(seed=7, target_messages=4000, out_dir=tmp_path / "b"))
    c = main.run(main.default_config(seed=8, target_messages=4000, out_dir=tmp_path / "c"))
    assert outputs(a) == outputs(b)
    assert all(x != y for x, y in zip(outputs(a), outputs(c)))


# ---------------------------------------------------------------------------
# Order flow sanity
# ---------------------------------------------------------------------------

def test_both_regimes_and_their_rates(big_run):
    config, result, _ = big_run
    first, last = result.arrivals[0][0].time_ns, result.arrivals[-1][0].time_ns
    time_in = {CALM: 0, BUSY: 0}
    for start, end, regime in result.regimes:
        overlap = min(end, last) - max(start, first)
        if overlap > 0:
            time_in[regime] += overlap
    assert time_in[CALM] > 0 and time_in[BUSY] > 0
    counts = {}
    for arrival, _ in result.arrivals:
        counts[(arrival.instrument, arrival.regime)] = counts.get((arrival.instrument, arrival.regime), 0) + 1
    for name, (calm_rate, busy_rate) in config.flow.rates.items():
        for regime, rate in ((CALM, calm_rate), (BUSY, busy_rate)):
            observed = counts[(name, regime)] / (time_in[regime] / 1e9)
            assert abs(observed - rate) / rate < 0.15, (name, regime, observed, rate)


def test_books_never_empty_for_long(big_run):
    _, result, _ = big_run
    assert max(result.max_empty_side_streak.values()) <= 25, result.max_empty_side_streak


def test_book_size_stays_bounded(big_run):
    # Cancels scale with live orders, so books settle near the target
    # instead of growing for the whole run.
    config, result, _ = big_run
    target = config.flow.target_live_orders
    assert all(target // 4 < n < 3 * target for n in result.final_resting.values()), \
        result.final_resting


def test_every_price_inside_the_band(big_run):
    config, result, _ = big_run
    band = {i.name: (i.low_limit, i.high_limit) for i in config.instruments}
    for arrival, instruction in result.arrivals:
        price = getattr(instruction, "price", getattr(instruction, "new_price", None))
        if price is not None:
            low, high = band[arrival.instrument]
            assert low <= price <= high, (arrival, instruction)
    for row in read_jsonl(result.intent_path):
        if row["templateId"] in (50, 51, 53):
            low, high = band[row["instrument"]]
            assert low <= row["fields"]["mDEntryPx"] <= high, row


def test_prices_stay_inside_a_tight_band(tmp_path):
    # Bands only 3 ticks either side of the previous close, so the flow
    # must clamp (the default +-8% band is rarely reached in a short run).
    import dataclasses
    base = main.default_config(seed=5, target_messages=3000, out_dir=tmp_path, record=True)
    tight = tuple(dataclasses.replace(i, low_limit=i.prev_close - 3 * i.tick,
                                      high_limit=i.prev_close + 3 * i.tick)
                  for i in base.instruments)
    config = dataclasses.replace(base, instruments=tight)
    result = main.run(config)
    band = {i.name: (i.low_limit, i.high_limit) for i in tight}
    at_limit = 0
    for arrival, instruction in result.arrivals:
        price = getattr(instruction, "price", getattr(instruction, "new_price", None))
        if price is not None:
            low, high = band[arrival.instrument]
            assert low <= price <= high, (arrival, instruction)
            at_limit += price in (low, high)
    assert at_limit > 100  # the clamp was actually exercised
    for row in read_jsonl(result.intent_path):
        if row["templateId"] in (50, 51, 53):
            low, high = band[row["instrument"]]
            assert low <= row["fields"]["mDEntryPx"] <= high, row


def test_every_instruction_kind_and_no_rejections(big_run):
    _, result, _ = big_run
    kinds = {"NewOrder", "Cancel", "SizeChange", "PriceChange", "IocOrder", "MarketOrder"}
    assert kinds <= set(result.instructions)
    assert all(result.instructions[k] > 100 for k in kinds), result.instructions
    assert result.outcomes["Rejected"] == 0
    assert result.outcomes["SizeReduced"] > 100 and result.outcomes["Requeued"] > 100
    assert result.outcomes["Executed"] > 500


def test_mid_between_ticks_rounds_both_ways():
    from b3gen.flow import OrderFlow, FlowInstrument
    inst = FlowInstrument("X", tick=100, lot=100, low_limit=1_000, high_limit=1_000_000,
                          reference_price=50_000)
    flow = OrderFlow([inst], FlowParams(rates={"X": (10, 100)}), seed=3, start_ns=0).flows["X"]
    # Bid 50_000, ask 50_100: the midpoint 50_050 falls between ticks.
    draws = [flow._mid([(50_000, 1, 1)], [(50_100, 1, 1)]) for _ in range(2000)]
    assert set(draws) == {50_000, 50_100}
    assert 0.45 < draws.count(50_100) / len(draws) < 0.55
    # Bid 50_000, ask 50_200: the midpoint is on a tick, no rounding.
    assert {flow._mid([(50_000, 1, 1)], [(50_200, 1, 1)]) for _ in range(50)} == {50_100}


def test_twenty_thousand_messages_run_quickly(big_run):
    _, result, seconds = big_run
    assert result.messages >= 20_000
    assert seconds < 30, f"{seconds:.1f} s"
