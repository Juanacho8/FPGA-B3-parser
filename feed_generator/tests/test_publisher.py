"""Publisher tests.

Hand cases check the exact message sequence per outcome. Seeded random
streams (tests/order_stream.py) then run engine -> publisher, and the
published bytes are checked independently: a book rebuilt from them
(tests/book_builder.py) must equal the engine's book after every event, and
statistics must equal a brute-force recompute from the decoded Trades.
"""

from fractions import Fraction

import pytest

from b3gen import encode
from b3gen.engine import (
    BUY, SELL, Cancel, Engine, IocOrder, MarketOrder, SupervisorDeleteThru, NewOrder, PriceChange,
    SizeChange,
)
from b3gen.publisher import Instrument, PublishError, Publisher, SessionValues, round_half_even
from book_builder import BookBuilder
from order_stream import BOOKS, random_stream, to_engine_instruction
import sbe_decoder

ORDER, DELETE, MASS, TRADE, SUMMARY, STATS = 50, 51, 52, 53, 55, 56
OPENING, HIGH, LOW, CLOSING, PBAND, QBAND, SETTLE, OI, PHASE = 15, 24, 25, 17, 22, 21, 28, 29, 10
EOE = 0x80
REGULAR, OPEN_BIT, SAME_BIT = 1 << 13, 1 << 0, 1 << 2

INSTRUMENTS = {
    "A": Instrument("A", 1001, 70, "EQT", "EQ1", 100, False),
    "B": Instrument("B", 1002, 70, "EQT", "EQ1", 100, False),
    "F": Instrument("F", 2001, 68, "DRV", "FUT", 50_000, True),
}
SECURITY = {i.security_id: name for name, i in INSTRUMENTS.items()}
PREV_CLOSE = {"A": 99_900, "B": 100_100, "F": 50_000_000}


def session_values(name):
    inst = INSTRUMENTS[name]
    fut = dict(settlement_price=PREV_CLOSE[name], settl_date=20_380, open_interest=12_345) \
        if inst.is_future else {}
    return SessionValues(prev_close=PREV_CLOSE[name], low_limit=PREV_CLOSE[name] // 2,
                         high_limit=PREV_CLOSE[name] * 2, avg_daily_traded_qty=0 if inst.is_future
                         else 1_000_000, max_trade_vol=500_000, **fut)


def setup(trade_date=20_381):
    engine = Engine([(n, tick, lot, seg) for n, (seg, tick, lot) in BOOKS.items()])
    pub = Publisher(INSTRUMENTS.values())
    start = pub.start_session(0, trade_date, {n: session_values(n) for n in INSTRUMENTS})
    return engine, pub, start


class Run:
    """Submit to book A and publish."""

    def __init__(self):
        self.engine, self.pub, _ = setup()
        self.t = 1000

    def __call__(self, cls, *args, book="A"):
        self.t += 10
        return self.pub.publish(self.engine.submit(book, cls(self.t, *args)))


def templates(msgs):
    return [m.template_id for m in msgs]


def eoe(msgs):
    return [m.fields.get("matchEventIndicator", 0) & EOE for m in msgs]


# ---------------------------------------------------------------------------
# Hand cases
# ---------------------------------------------------------------------------

def test_rested_order_new():
    r = Run()
    (m,) = r(NewOrder, BUY, 1000, 300, 7)
    assert m.template_id == ORDER and m.channel == 70 and m.instrument == "A"
    f = m.fields
    assert (f["mDUpdateAction"], f["mDEntryType"], f["mDEntryPx"], f["mDEntrySize"]) == (0, "0", 1000, 300)
    assert (f["enteringFirm"], f["mDInsertTimestamp"], f["transactTime"]) == (7, r.t, r.t)
    assert f["matchEventIndicator"] == EOE and "mDEntryPrevSize" not in f


def test_cancel_is_delete_order():
    r = Run()
    (new,) = r(NewOrder, SELL, 1000, 300, 7)
    (m,) = r(Cancel, 1)
    assert m.template_id == DELETE
    assert (m.fields["mDEntryType"], m.fields["mDEntryPx"], m.fields["mDEntrySize"],
            m.fields["secondaryOrderID"]) == ("1", 1000, 300, new.fields["secondaryOrderID"])


def test_size_reduction_is_order_change_with_prev_size():
    r = Run()
    (new,) = r(NewOrder, BUY, 1000, 300, 7)
    (m,) = r(SizeChange, 1, 200)
    f = m.fields
    assert (f["mDUpdateAction"], f["mDEntrySize"], f["mDEntryPrevSize"]) == (1, 200, 300)
    assert f["secondaryOrderID"] == new.fields["secondaryOrderID"]
    assert f["mDInsertTimestamp"] == new.fields["mDInsertTimestamp"]


def test_requeue_is_delete_then_new_with_new_soid():
    r = Run()
    (new,) = r(NewOrder, BUY, 1000, 300, 7)
    msgs = r(PriceChange, 1, 900)
    assert templates(msgs) == [DELETE, ORDER] and eoe(msgs) == [0, EOE]
    old, nxt = msgs[0].fields, msgs[1].fields
    assert old["secondaryOrderID"] == new.fields["secondaryOrderID"] and old["mDEntryPx"] == 1000
    assert nxt["secondaryOrderID"] > old["secondaryOrderID"] and nxt["mDEntryPx"] == 900
    assert nxt["mDInsertTimestamp"] == r.t and nxt["mDUpdateAction"] == 0


def test_supervisor_delete_thru():
    r = Run()
    r(NewOrder, BUY, 1000, 300, 7)
    r(NewOrder, BUY, 900, 300, 7)
    (m,) = r(SupervisorDeleteThru, BUY)
    assert m.template_id == MASS and (m.fields["mDUpdateAction"], m.fields["mDEntryType"]) == (3, "0")
    assert r(SupervisorDeleteThru, BUY) == []  # empty side: nothing


def test_unfilled_and_rejected_publish_nothing():
    r = Run()
    assert r(IocOrder, BUY, 1000, 100, 1) == []
    assert r(MarketOrder, SELL, 100, 1) == []
    assert r(Cancel, 999) == []


def test_sweep_with_partial_fill_of_last_level_and_remainder_resting():
    r = Run()
    r(NewOrder, SELL, 1000, 100, 1)
    r(NewOrder, SELL, 1000, 100, 2)
    r(NewOrder, SELL, 1100, 300, 3)
    msgs = r(NewOrder, BUY, 1100, 600, 9, book="A")
    # Fills: 100@1000, 100@1000, 300@1100 -> 500, remainder 100 rests at 1100.
    assert templates(msgs) == [SUMMARY, TRADE, TRADE, TRADE, DELETE, DELETE, DELETE, ORDER,
                               OPENING, HIGH, LOW, STATS]
    assert eoe(msgs) == [0] * 11 + [EOE]
    summary = msgs[0].fields
    assert (summary["aggressorSide"], summary["lastPx"], summary["fillQty"]) == (1, 1100, 500)
    assert summary["aggressorTime"] == r.t
    assert (summary["tradedHiddenQty"], summary["cxlQty"]) == (0, 0)  # limit: remainder rests
    trades = [m.fields for m in msgs if m.template_id == TRADE]
    assert [(t["mDEntryPx"], t["mDEntrySize"], t["tradeID"]) for t in trades] == \
        [(1000, 100, 1), (1000, 100, 2), (1100, 300, 3)]
    assert [t["tradeCondition"] for t in trades] == \
        [REGULAR | OPEN_BIT, REGULAR | SAME_BIT, REGULAR]
    assert [(t["mDEntryBuyer"], t["mDEntrySeller"]) for t in trades] == [(9, 1), (9, 2), (9, 3)]
    remainder = msgs[7].fields
    assert (remainder["mDUpdateAction"], remainder["mDEntryPx"], remainder["mDEntrySize"]) == (0, 1100, 100)
    stats = msgs[-1].fields
    assert (stats["tradeVolume"], stats["numberOfTrades"]) == (500, 3)
    assert stats["vwapPx"] == round_half_even(1000 * 200 + 1100 * 300, 500)
    assert stats["netChgPrevDay"] == (1100 - PREV_CLOSE["A"]) * 10_000
    assert msgs[8].fields["mDEntryPx"] == 1000  # opening = first fill
    assert (msgs[9].fields["mDEntryPx"], msgs[10].fields["mDEntryPx"]) == (1100, 1000)


def test_sweep_with_partial_fill_of_last_level_no_remainder():
    r = Run()
    # Book A's lot is 100, so the 100/100/300 vs 450 case is scaled by 10.
    r(NewOrder, SELL, 1000, 1000, 1)
    r(NewOrder, SELL, 1000, 1000, 2)
    (last,) = r(NewOrder, SELL, 1100, 3000, 3)
    msgs = r(NewOrder, BUY, 1100, 4500, 9)
    # Fills: 1000@1000, 1000@1000, 2500@1100. The 1100 order keeps 500; nothing rests.
    assert templates(msgs) == [SUMMARY, TRADE, TRADE, TRADE, DELETE, DELETE, ORDER,
                               OPENING, HIGH, LOW, STATS]
    deletes = [m.fields for m in msgs if m.template_id == DELETE]
    assert [(d["mDEntryPx"], d["mDEntrySize"]) for d in deletes] == [(1000, 1000), (1000, 1000)]
    (change,) = [m.fields for m in msgs if m.template_id == ORDER]
    assert (change["mDUpdateAction"], change["mDEntrySize"], change["mDEntryPrevSize"]) == (1, 500, 3000)
    assert change["secondaryOrderID"] == last.fields["secondaryOrderID"]
    assert change["mDEntryPx"] == 1100
    assert msgs[0].fields["fillQty"] == 4500 and msgs[0].fields["lastPx"] == 1100


def test_partial_fill_of_resting_is_order_change():
    r = Run()
    (resting,) = r(NewOrder, SELL, 1000, 500, 1)
    msgs = r(NewOrder, BUY, 1000, 200, 2)
    change = next(m.fields for m in msgs if m.template_id == ORDER)
    assert (change["mDUpdateAction"], change["mDEntrySize"], change["mDEntryPrevSize"]) == (1, 300, 500)
    assert change["secondaryOrderID"] == resting.fields["secondaryOrderID"]
    assert change["mDInsertTimestamp"] == resting.fields["mDInsertTimestamp"]
    assert change["enteringFirm"] == 1


def test_later_event_sends_high_low_only_when_changed():
    r = Run()
    r(NewOrder, SELL, 1000, 100, 1)
    r(NewOrder, SELL, 1000, 100, 1)
    r(NewOrder, SELL, 1200, 100, 1)
    r(NewOrder, BUY, 1000, 100, 2)                 # first trade: open, high, low
    assert templates(r(NewOrder, BUY, 1000, 100, 2)) == [SUMMARY, TRADE, DELETE, STATS]
    msgs = r(NewOrder, BUY, 1200, 100, 2)          # new high only
    assert templates(msgs) == [SUMMARY, TRADE, DELETE, HIGH, STATS]
    assert msgs[3].fields["mDEntryPx"] == 1200


def test_crossing_price_change():
    r = Run()
    r(NewOrder, SELL, 1100, 100, 1)
    (old,) = r(NewOrder, BUY, 1000, 300, 2)
    msgs = r(PriceChange, 2, 1100)
    assert templates(msgs) == [SUMMARY, DELETE, TRADE, DELETE, ORDER, OPENING, HIGH, LOW, STATS]
    replaced = msgs[1].fields
    assert (replaced["secondaryOrderID"], replaced["mDEntryPx"], replaced["mDEntrySize"]) == \
        (old.fields["secondaryOrderID"], 1000, 300)
    remainder = msgs[4].fields
    assert remainder["mDEntrySize"] == 200 and remainder["mDEntryPx"] == 1100
    assert remainder["secondaryOrderID"] > old.fields["secondaryOrderID"]


def test_market_order_execution():
    r = Run()
    r(NewOrder, BUY, 1000, 100, 1)
    msgs = r(MarketOrder, SELL, 300, 2)  # remainder 200 discarded: cxlQty
    assert templates(msgs) == [SUMMARY, TRADE, DELETE, OPENING, HIGH, LOW, STATS]
    assert msgs[0].fields["aggressorSide"] == 2
    assert (msgs[0].fields["cxlQty"], msgs[0].fields["tradedHiddenQty"]) == (200, 0)
    assert (msgs[1].fields["mDEntryBuyer"], msgs[1].fields["mDEntrySeller"]) == (1, 2)


def test_rpt_seq_per_instrument_and_only_where_the_template_has_it():
    r = Run()
    a1 = r(NewOrder, BUY, 1000, 100, 1)
    b1 = r(NewOrder, BUY, 1000, 100, 1, book="B")
    a2 = r(NewOrder, BUY, 900, 100, 1)
    # Session start sent ClosingPrice, PriceBand, QuantityBand for each: rptSeq 1-3.
    assert [m.fields["rptSeq"] for m in (a1 + a2)] == [4, 5]
    assert b1[0].fields["rptSeq"] == 4
    msgs = r(NewOrder, SELL, 1000, 100, 2)
    assert "rptSeq" in msgs[0].fields  # ExecutionSummary has rptSeq
    assert [m.fields["rptSeq"] for m in msgs] == list(range(6, 6 + len(msgs)))


def test_session_start_sequence():
    _, _, start = setup()
    assert [(m.template_id, m.instrument) for m in start] == [
        (PHASE, None), (PHASE, None),
        (CLOSING, "A"), (PBAND, "A"), (QBAND, "A"),
        (CLOSING, "B"), (PBAND, "B"), (QBAND, "B"),
        (CLOSING, "F"), (PBAND, "F"), (QBAND, "F"), (SETTLE, "F"), (OI, "F")]
    assert all(m.fields["matchEventIndicator"] == EOE for m in start)  # each standalone
    phase = start[0].fields
    assert (phase["securityGroup"], phase["tradingSessionSubID"], phase["securityTradingEvent"]) == \
        ("EQ1", 17, 4)
    assert (start[0].channel, start[1].channel, start[1].fields["securityGroup"]) == (70, 68, "FUT")
    closing = start[2].fields
    assert closing["mDEntryPx"] == PREV_CLOSE["A"] * 10_000 and closing["openCloseSettlFlag"] == 4
    settle = start[11].fields
    assert (settle["settlPriceType"], settle["priceType"], settle["settlDate"]) == (1, 2, 20_380)
    assert "openCloseSettlFlag" not in settle  # deprecated, written as null


def test_session_end_and_reset_on_new_session():
    r = Run()
    r(NewOrder, SELL, 1000, 100, 1)
    r(NewOrder, BUY, 1000, 100, 2)
    end = r.pub.end_session(5000)
    assert [m.template_id for m in end] == [CLOSING] * 3
    closes = {m.instrument: m.fields for m in end}
    assert closes["A"]["mDEntryPx"] == 1000 * 10_000 and closes["A"]["openCloseSettlFlag"] == 0
    assert closes["B"]["mDEntryPx"] == PREV_CLOSE["B"] * 10_000  # no trades: previous close
    with pytest.raises(PublishError):
        r.pub.publish(r.engine.submit("A", NewOrder(6000, BUY, 900, 100, 1)))
    r.pub.start_session(7000, 20_382, {n: session_values(n) for n in INSTRUMENTS})
    r.t = 7000
    r(NewOrder, SELL, 1000, 100, 1)
    msgs = r(NewOrder, BUY, 1000, 100, 2)
    trade = next(m.fields for m in msgs if m.template_id == TRADE)
    assert trade["tradeID"] == 1 and trade["tradeDate"] == 20_382
    assert trade["tradeCondition"] == REGULAR | OPEN_BIT  # new session: opening again
    stats = msgs[-1].fields
    assert (stats["tradeVolume"], stats["numberOfTrades"]) == (100, 1)


def test_round_half_even():
    assert [round_half_even(n, 2) for n in (1, 3, 5, 7)] == [0, 2, 2, 4]
    assert round_half_even(10, 3) == 3 and round_half_even(11, 3) == 4


# ---------------------------------------------------------------------------
# Random streams: engine -> publisher, checked from the bytes
# ---------------------------------------------------------------------------

class Recompute:
    """Brute-force statistics from decoded Trades of the current session."""

    def __init__(self):
        self.trades = {}

    def reset(self):
        self.trades = {sid: [] for sid in SECURITY}

    def add(self, sid, price, qty):
        self.trades[sid].append((price, qty))

    def stats(self, sid):
        trades = self.trades[sid]
        volume = sum(q for _, q in trades)
        prev = PREV_CLOSE[SECURITY[sid]]
        return {"tradeVolume": volume, "numberOfTrades": len(trades),
                "vwapPx": round(Fraction(sum(p * q for p, q in trades), volume)),
                "netChgPrevDay": (trades[-1][0] - prev) * 10_000}

    def high(self, sid):
        return max(p for p, _ in self.trades[sid])

    def low(self, sid):
        return min(p for p, _ in self.trades[sid])


def run_stream(seed, steps):
    engine, pub, start = setup()
    builder = BookBuilder()
    recompute = Recompute()
    recompute.reset()
    rpt = {}
    counts = {"events": 0, "matching": 0, "high": 0, "low": 0, "cancelled": 0}

    def check_event(msgs, time_ns, expected=None):
        if not msgs:
            return
        counts["events"] += 1
        # Exactly one EndOfEvent, on the last message.
        assert eoe(msgs) == [0] * (len(msgs) - 1) + [EOE], templates(msgs)
        decoded = []
        for m in msgs:
            assert m.data == encode.encode(m.template_id, m.fields)
            header, fields = sbe_decoder.decode(m.data)
            assert header["templateId"] == m.template_id
            assert all(fields[k] == v for k, v in m.fields.items()), m
            assert m.time_ns == time_ns
            if m.instrument is not None:
                assert m.channel == INSTRUMENTS[m.instrument].channel
            decoded.append((header["templateId"], fields))
            if "rptSeq" in fields:
                sid = fields["securityID"]
                assert fields["rptSeq"] == rpt.get(sid, 0) + 1, "rptSeq gap"
                rpt[sid] = fields["rptSeq"]
            builder.apply(m.data)
        tids = [t for t, _ in decoded]
        if TRADE in tids:
            counts["matching"] += 1
            assert tids[0] == SUMMARY and tids[-1] == STATS
            sid = decoded[0][1]["securityID"]
            high_before = recompute.high(sid) if recompute.trades[sid] else None
            low_before = recompute.low(sid) if recompute.trades[sid] else None
            first = not recompute.trades[sid]
            trades = [f for t, f in decoded if t == TRADE]
            for f in trades:
                recompute.add(sid, f["mDEntryPx"], f["mDEntrySize"])
            assert decoded[0][1]["fillQty"] == sum(f["mDEntrySize"] for f in trades)
            assert decoded[0][1]["lastPx"] == trades[-1]["mDEntryPx"]
            # The reference engine's discarded remainder (market/IOC), else 0.
            assert decoded[0][1]["tradedHiddenQty"] == 0
            assert decoded[0][1]["cxlQty"] == expected[1]["discarded_qty"]
            if expected[1]["discarded_qty"]:
                counts["cancelled"] += 1
            stats = decoded[-1][1]
            assert {k: stats[k] for k in recompute.stats(sid)} == recompute.stats(sid)
            assert stats["tradeVolume"] == sum(q for _, q in recompute.trades[sid])
            by_tid = {t: f for t, f in decoded}
            assert (OPENING in by_tid) == first
            assert (HIGH in by_tid) == (first or recompute.high(sid) != high_before)
            assert (LOW in by_tid) == (first or recompute.low(sid) != low_before)
            if HIGH in by_tid:
                counts["high"] += 1
                assert by_tid[HIGH]["mDEntryPx"] == recompute.high(sid)
            if LOW in by_tid:
                counts["low"] += 1
                assert by_tid[LOW]["mDEntryPx"] == recompute.low(sid)
        else:
            assert SUMMARY not in tids and STATS not in tids

    def check_books():
        for name, inst in INSTRUMENTS.items():
            expected = [(o.side, o.price, o.qty, o.soid)
                        for o in engine.books[name].resting_orders()]
            assert builder.resting(inst.security_id) == expected, name

    for m in start:
        check_event([m], 0)
    half = steps // 2
    for i, (instr, expected, _) in enumerate(random_stream(seed, steps)):
        if i == half:
            # A new trading session halfway through: statistics reset.
            t = instr[2]
            for m in pub.end_session(t):
                check_event([m], t)
            for m in pub.start_session(t, 20_382, {n: session_values(n) for n in INSTRUMENTS}):
                check_event([m], t)
            recompute.reset()
        outcome = engine.submit(instr[1], to_engine_instruction(instr))
        check_event(pub.publish(outcome), instr[2], expected)
        check_books()
    return counts


@pytest.mark.parametrize("seed", [1, 2])
def test_random_stream_book_replay_and_event_rules(seed):
    counts = run_stream(seed, steps=4000)
    # About half of what the seeds produce (~650 matching events, ~30 new
    # highs and ~35 new lows each), so a weaker stream fails.
    assert counts["matching"] > 300 and counts["high"] > 14 and counts["low"] > 14
    assert counts["cancelled"] > 20


def test_publishing_is_deterministic():
    def run():
        engine, pub, start = setup()
        out = [m.data for m in start]
        for instr, _, _ in random_stream(5, 1500):
            out += [m.data for m in pub.publish(engine.submit(instr[1], to_engine_instruction(instr)))]
        return out
    assert run() == run()


def half_even(value):
    """Independent rounding of a Fraction to an integer, ties to even."""
    floor = value.numerator // value.denominator
    rest = value - floor
    if rest > Fraction(1, 2):
        return floor + 1
    if rest == Fraction(1, 2):
        return floor + floor % 2
    return floor


def test_vwap_exact_ties_round_half_to_even_end_to_end():
    # Tick 1 and lot 1, so VWAPs land on exact .5 ties: 3/2 = 1.5 (half-even
    # 2, half-down 1) and then 15/6 = 2.5 (half-even 2, half-up 3). Engine ->
    # publisher -> bytes -> independent decoder, recomputed from the Trades.
    inst = Instrument("T", 3001, 70, "EQT", "EQ1", 1, False)
    engine = Engine([("T", 1, 1, "EQT")])
    pub = Publisher([inst])
    pub.start_session(0, 20_381, {"T": SessionValues(prev_close=2, low_limit=1, high_limit=10,
                                                     avg_daily_traded_qty=0, max_trade_vol=100)})
    trades, vwaps = [], []

    def submit(t, instruction):
        for m in pub.publish(engine.submit("T", instruction)):
            header, fields = sbe_decoder.decode(m.data)
            if header["templateId"] == TRADE:
                trades.append((fields["mDEntryPx"], fields["mDEntrySize"]))
            elif header["templateId"] == STATS:
                exact = Fraction(sum(p * q for p, q in trades), sum(q for _, q in trades))
                vwaps.append((exact, fields["vwapPx"]))

    submit(1, NewOrder(1, SELL, 1, 1, 1))
    submit(2, NewOrder(2, SELL, 2, 1, 1))
    submit(3, NewOrder(3, BUY, 2, 2, 2))   # 1@1 + 1@2: VWAP 1.5
    submit(4, NewOrder(4, SELL, 3, 4, 1))
    submit(5, NewOrder(5, BUY, 3, 4, 2))   # + 4@3: VWAP 15/6 = 2.5
    assert [exact for exact, _ in vwaps] == [Fraction(3, 2), Fraction(5, 2)]  # both ties
    assert [wire for _, wire in vwaps] == [half_even(exact) for exact, _ in vwaps] == [2, 2]
