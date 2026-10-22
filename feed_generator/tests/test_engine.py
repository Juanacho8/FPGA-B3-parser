"""Matching engine tests.

Hand-written cases for each rule, then seeded random instruction streams
compared step by step against tests/reference_engine.py, a brute-force
engine that shares no code with b3gen.engine. check_invariants() runs after
every instruction.
"""

import random
from collections import Counter as Tally
from dataclasses import asdict

import pytest

from b3gen.engine import (
    BUY, SELL, Cancel, Cancelled, Engine, EngineError, Executed, IocOrder, MarketOrder,
    MassCancel, MassCancelled, NewOrder, PriceChange, Rejected, Requeued, Rested,
    SizeChange, SizeReduced, Unfilled,
)
from reference_engine import ReferenceEngine

TICK = 100   # 0.01 in x10^-4 units
LOT = 100


def make_engine(**kw):
    return Engine([("A", TICK, LOT, "EQT"), ("B", TICK, LOT, "EQT"),
                   ("F", 50_000, 1, "DRV")], **kw)


class Driver:
    """Submit to one book and check invariants after every instruction."""

    def __init__(self, engine=None, book="A"):
        self.engine = engine or make_engine()
        self.book = book
        self.t = 0

    def __call__(self, cls, *args):
        self.t += 10
        outcome = self.engine.submit(self.book, cls(self.t, *args))
        self.engine.books[self.book].check_invariants()
        return outcome

    def top(self, n=5):
        return self.engine.books[self.book].top(n)


# ---------------------------------------------------------------------------
# Hand cases
# ---------------------------------------------------------------------------

def test_rests_when_not_crossing():
    d = Driver()
    out = d(NewOrder, BUY, 1000, 300, 7)
    assert out == Rested("A", 10, handle=1, soid=1, side=BUY, price=1000, qty=300,
                         firm=7, insert_time_ns=10)
    assert d.top() == ([(1000, 300, 1)], [])


def test_priority_within_a_price():
    d = Driver()
    first = d(NewOrder, SELL, 1000, 100, 1)
    second = d(NewOrder, SELL, 1000, 100, 2)
    out = d(NewOrder, BUY, 1000, 100, 3)
    assert isinstance(out, Executed)
    assert [f.resting_handle for f in out.fills] == [first.handle]
    assert [o.handle for o in d.engine.books["A"].resting_orders()] == [second.handle]


def test_better_price_beats_earlier_order():
    d = Driver()
    d(NewOrder, SELL, 1100, 100, 1)
    better = d(NewOrder, SELL, 1000, 100, 2)
    out = d(NewOrder, BUY, 1100, 100, 3)
    assert out.fills[0].resting_handle == better.handle and out.fills[0].price == 1000


def test_partial_fill_of_resting_order_keeps_its_soid():
    d = Driver()
    resting = d(NewOrder, SELL, 1000, 500, 1)
    out = d(NewOrder, BUY, 1000, 200, 2)
    (fill,) = out.fills
    assert (fill.resting_soid, fill.qty, fill.resting_qty_before, fill.resting_qty_after) == \
        (resting.soid, 200, 500, 300)
    assert out.remainder is None and out.filled_qty == 200
    (view,) = d.engine.books["A"].resting_orders()
    assert (view.soid, view.qty, view.insert_time_ns) == (resting.soid, 300, resting.insert_time_ns)


def test_sweep_across_levels_then_rest():
    d = Driver()
    d(NewOrder, SELL, 1000, 100, 1)
    d(NewOrder, SELL, 1100, 200, 1)
    d(NewOrder, SELL, 1200, 300, 1)
    out = d(NewOrder, BUY, 1100, 500, 2)
    assert [(f.price, f.qty) for f in out.fills] == [(1000, 100), (1100, 200)]
    assert out.filled_qty == 300
    assert out.remainder.price == 1100 and out.remainder.qty == 200
    assert out.remainder.soid == out.soid  # rests under its entry soid
    assert d.top() == ([(1100, 200, 1)], [(1200, 300, 1)])


def test_soid_allocated_at_entry_even_if_fully_filled():
    d = Driver()
    d(NewOrder, SELL, 1000, 100, 1)              # soid 1
    filled = d(NewOrder, BUY, 1000, 100, 2)      # soid 2, fully filled
    nxt = d(NewOrder, BUY, 900, 100, 2)          # soid 3
    assert filled.soid == 2 and filled.remainder is None
    assert nxt.soid == 3


def test_cancel():
    d = Driver()
    o = d(NewOrder, BUY, 1000, 300, 7)
    out = d(Cancel, o.handle)
    assert isinstance(out, Cancelled) and out.order.qty == 300 and out.order.soid == o.soid
    assert d.top() == ([], [])


def test_size_down_keeps_soid_and_priority():
    d = Driver()
    first = d(NewOrder, BUY, 1000, 300, 1)
    d(NewOrder, BUY, 1000, 100, 2)
    out = d(SizeChange, first.handle, 200)
    assert out == SizeReduced("A", d.t, first.handle, first.soid, BUY, 1000, 300, 200, 1,
                              first.insert_time_ns)
    assert d.engine.books["A"].resting_orders()[0].handle == first.handle


def test_size_up_requeues_with_new_soid():
    d = Driver()
    first = d(NewOrder, BUY, 1000, 300, 1)
    second = d(NewOrder, BUY, 1000, 100, 2)
    out = d(SizeChange, first.handle, 400)
    assert isinstance(out, Requeued)
    assert out.old_soid == first.soid and out.new_soid > second.soid
    assert (out.old_qty, out.new_qty, out.new_insert_time_ns) == (300, 400, d.t)
    assert [o.handle for o in d.engine.books["A"].resting_orders()] == [second.handle, first.handle]


def test_non_crossing_price_change_requeues():
    d = Driver()
    o = d(NewOrder, BUY, 1000, 300, 1)
    d(NewOrder, BUY, 900, 100, 2)
    out = d(PriceChange, o.handle, 900)
    assert isinstance(out, Requeued) and (out.old_price, out.new_price) == (1000, 900)
    assert out.new_soid > out.old_soid
    assert d.top() == ([(900, 400, 2)], [])


def test_crossing_price_change_executes():
    d = Driver()
    d(NewOrder, SELL, 1100, 100, 1)
    o = d(NewOrder, BUY, 1000, 300, 2)
    out = d(PriceChange, o.handle, 1100)
    assert isinstance(out, Executed)
    assert out.replaced.soid == o.soid and out.replaced.price == 1000
    assert out.handle == o.handle and out.soid > o.soid
    assert out.fills[0].price == 1100 and out.filled_qty == 100
    assert out.remainder.qty == 200 and out.remainder.soid == out.soid
    assert out.remainder.insert_time_ns == d.t
    assert d.top() == ([(1100, 200, 1)], [])


def test_market_order_remainder_discarded():
    d = Driver()
    d(NewOrder, SELL, 1000, 100, 1)
    d(NewOrder, SELL, 5000, 100, 1)  # no price protection
    out = d(MarketOrder, BUY, 300, 2)
    assert [f.price for f in out.fills] == [1000, 5000]
    assert out.price is None and out.discarded_qty == 100 and out.remainder is None
    assert d.top() == ([], [])


def test_ioc_remainder_discarded():
    d = Driver()
    d(NewOrder, SELL, 1000, 100, 1)
    d(NewOrder, SELL, 1100, 100, 1)
    out = d(IocOrder, BUY, 1000, 300, 2)
    assert out.filled_qty == 100 and out.discarded_qty == 200 and out.remainder is None
    assert d.top() == ([], [(1100, 100, 1)])


def test_unfilled_ioc_and_market():
    d = Driver()
    d(NewOrder, SELL, 1100, 100, 1)
    ioc = d(IocOrder, BUY, 1000, 100, 2)
    mkt = d(MarketOrder, SELL, 100, 2)  # no bids at all
    assert isinstance(ioc, Unfilled) and ioc.discarded_qty == 100
    assert isinstance(mkt, Unfilled) and mkt.price is None
    assert d.top() == ([], [(1100, 100, 1)])


def test_mass_cancel_one_side_in_priority_order():
    d = Driver()
    a = d(NewOrder, BUY, 900, 100, 1)
    b = d(NewOrder, BUY, 1000, 100, 1)
    c = d(NewOrder, BUY, 1000, 100, 1)
    d(NewOrder, SELL, 1200, 100, 1)
    out = d(MassCancel, BUY)
    assert isinstance(out, MassCancelled)
    assert [o.handle for o in out.orders] == [b.handle, c.handle, a.handle]
    assert d.top() == ([], [(1200, 100, 1)])
    assert d(MassCancel, BUY).orders == ()


def test_rejections_on_unknown_and_gone_handles():
    d = Driver()
    o = d(NewOrder, BUY, 1000, 100, 1)
    d(Cancel, o.handle)
    for instruction in ((Cancel, o.handle), (SizeChange, o.handle, 200),
                        (PriceChange, o.handle, 900), (Cancel, 999)):
        out = d(*instruction)
        assert isinstance(out, Rejected) and out.reason == "unknown or gone handle"
    filled = d(NewOrder, SELL, 1000, 100, 1)
    d(NewOrder, BUY, 1000, 100, 2)
    assert isinstance(d(Cancel, filled.handle), Rejected)


def test_handle_of_other_book_is_rejected():
    engine = make_engine()
    o = engine.submit("A", NewOrder(1, BUY, 1000, 100, 1))
    assert isinstance(engine.submit("B", Cancel(2, o.handle)), Rejected)


@pytest.mark.parametrize("instruction", [
    NewOrder(1, BUY, 1050, 100, 1),     # off tick
    NewOrder(1, BUY, 0, 100, 1),        # not positive
    NewOrder(1, BUY, 1000, 150, 1),     # not a lot multiple
    NewOrder(1, BUY, 1000, 0, 1),
    IocOrder(1, SELL, 1000, -100, 1),
    MarketOrder(1, BUY, 50, 1),
    NewOrder(1, "HOLD", 1000, 100, 1),  # bad side
    NewOrder(1, BUY, 1000.0, 100, 1),   # float price
])
def test_invalid_input_raises(instruction):
    with pytest.raises(EngineError):
        make_engine().submit("A", instruction)


def test_invalid_changes_raise():
    d = Driver()
    o = d(NewOrder, BUY, 1000, 300, 1)
    for instruction in ((SizeChange, o.handle, 0), (SizeChange, o.handle, 300),
                        (SizeChange, o.handle, 250), (PriceChange, o.handle, 1000),
                        (PriceChange, o.handle, 1001)):
        with pytest.raises(EngineError):
            d(*instruction)


def test_time_going_backwards_raises():
    engine = make_engine()
    engine.submit("A", NewOrder(100, BUY, 1000, 100, 1))
    with pytest.raises(EngineError):
        engine.submit("A", NewOrder(99, BUY, 1000, 100, 1))


def test_soid_counter_shared_within_segment_only():
    engine = make_engine()
    a = engine.submit("A", NewOrder(1, BUY, 1000, 100, 1))
    b = engine.submit("B", NewOrder(1, BUY, 1000, 100, 1))
    f = engine.submit("F", NewOrder(1, BUY, 50_000, 1, 1))
    a2 = engine.submit("A", NewOrder(2, BUY, 1000, 100, 1))
    assert (a.soid, b.soid, a2.soid) == (1, 2, 3)  # A and B share EQT's counter
    assert f.soid == 1                             # DRV has its own
    assert len({a.handle, b.handle, f.handle, a2.handle}) == 4  # handles engine-wide


# ---------------------------------------------------------------------------
# Random streams vs the brute-force reference
# ---------------------------------------------------------------------------

BOOKS = {"A": ("EQT", TICK, LOT), "B": ("EQT", TICK, LOT), "F": ("DRV", 50_000, 1)}


def random_stream(seed, steps):
    """Yield (instruction tuple, reference outcome, reference). The stream
    applies each instruction to its own reference, so size and price changes
    are always valid (or hit gone handles on purpose)."""
    rng = random.Random(seed)
    ref = ReferenceEngine({name: seg for name, (seg, _, _) in BOOKS.items()})
    mid = {name: 1000 * tick for name, (_, tick, _) in BOOKS.items()}
    handles = {name: [] for name in BOOKS}
    t = 0
    for _ in range(steps):
        t += rng.randrange(2000)
        book = rng.choice(list(BOOKS))
        _, tick, lot = BOOKS[book]
        if rng.random() < 0.1:
            mid[book] += rng.choice((-tick, tick))
        side = rng.choice(("BUY", "SELL"))
        sign = 1 if side == "BUY" else -1
        r = rng.random()
        if r < 0.32:      # passive limit
            instr = ("new", book, t, side, mid[book] - sign * tick * rng.randint(1, 6),
                     lot * rng.randint(1, 10), rng.randint(1, 5))
        elif r < 0.40:    # aggressive limit, may sweep
            instr = ("new", book, t, side, mid[book] + sign * tick * rng.randint(0, 5),
                     lot * rng.randint(5, 40), rng.randint(1, 5))
        elif r < 0.47:    # IOC, crossing or not
            instr = ("ioc", book, t, side, mid[book] + sign * tick * rng.randint(-3, 3),
                     lot * rng.randint(1, 30), rng.randint(1, 5))
        elif r < 0.50:    # small market order
            instr = ("market", book, t, side, lot * rng.randint(1, 6), rng.randint(1, 5))
        elif r < 0.505:   # mass cancel one side
            instr = ("mass", book, t, side)
        else:
            # Mostly live orders, sometimes any handle ever issued (often gone),
            # occasionally one never issued.
            live = [v["handle"] for v in ref.resting_orders(book)]
            r3 = rng.random()
            if live and r3 < 0.8:
                handle = rng.choice(live)
            elif handles[book] and r3 < 0.97:
                handle = rng.choice(handles[book])
            else:
                handle = 10 ** 9
            o = ref.order(book, handle)
            r2 = rng.random()
            if r2 < 0.34:
                instr = ("cancel", book, t, handle)
            elif r2 < 0.67:
                choices = [q for q in range(lot, lot * 15, lot) if o is None or q != o["qty"]]
                instr = ("size", book, t, handle, rng.choice(choices))
            else:
                base = o["price"] if o else mid[book]
                if o and rng.random() < 0.3:
                    # Push toward the other side, often far enough to cross.
                    toward = 1 if o["side"] == "BUY" else -1
                    k = toward * rng.randint(3, 10)
                else:
                    k = rng.choice([k for k in range(-6, 7) if k])
                if base + k * tick <= 0:
                    k = abs(k)
                instr = ("price", book, t, handle, base + k * tick)
        if instr[0] in ("new", "ioc", "market"):
            handles[book].append(ref.next_handle)
        # The stream keeps its own reference up to date, so it is valid on
        # its own; callers get the reference's outcome alongside.
        expected = ref.apply(instr)
        yield instr, expected, ref


def to_engine_instruction(instr):
    kind, _, t = instr[:3]
    args = instr[3:]
    return {"new": NewOrder, "ioc": IocOrder, "market": MarketOrder, "cancel": Cancel,
            "size": SizeChange, "price": PriceChange, "mass": MassCancel}[kind](t, *args)


def normalize(outcome):
    return type(outcome).__name__, asdict(outcome)


def run_comparison(seed, steps):
    engine = Engine([(name, tick, lot, seg) for name, (seg, tick, lot) in BOOKS.items()])
    tally = Tally()
    for instr, expected, ref in random_stream(seed, steps):
        book = instr[1]
        got = normalize(engine.submit(book, to_engine_instruction(instr)))
        assert got == expected, (instr, got, expected)
        b = engine.books[book]
        b.check_invariants()
        assert b.top(5) == ref.top(book, 5)
        assert [asdict(v) for v in b.resting_orders()] == ref.resting_orders(book)
        tally_outcome(tally, got)
    return tally


def tally_outcome(tally, normalized):
    kind, data = normalized
    tally[kind] += 1
    if kind == "Executed":
        if len({f["price"] for f in data["fills"]}) >= 2:
            tally["sweep_2plus_levels"] += 1
        if any(f["resting_qty_after"] > 0 for f in data["fills"]):
            tally["partial_fill_of_resting"] += 1
        if data["replaced"] is not None:
            tally["crossing_price_change"] += 1
        if data["order_type"] == "ioc" and data["discarded_qty"] > 0:
            tally["ioc_partial_discard"] += 1
        if data["order_type"] == "market":
            tally["market_executed"] += 1
    if kind == "Rejected" and data["handle"] != 10 ** 9:
        tally["rejected_gone_handle"] += 1
    if kind == "MassCancelled" and data["orders"]:
        tally["mass_cancel_nonempty"] += 1


# About half of what each seed actually produces, so a weaker stream fails.
MINIMUMS = {
    "Rested": 900, "Executed": 450, "Cancelled": 400, "SizeReduced": 150,
    "Requeued": 500, "Rejected": 250, "Unfilled": 120,
    "sweep_2plus_levels": 110, "partial_fill_of_resting": 280,
    "crossing_price_change": 70, "ioc_partial_discard": 30, "market_executed": 70,
    "rejected_gone_handle": 150, "mass_cancel_nonempty": 14,
}


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_stream_matches_reference(seed):
    tally = run_comparison(seed, steps=6000)
    short = {k: (tally[k], n) for k, n in MINIMUMS.items() if tally[k] < n}
    assert not short, f"cases not exercised enough (got, wanted): {short}"


def test_same_instructions_give_identical_outcomes():
    def run():
        engine = Engine([(name, tick, lot, seg) for name, (seg, tick, lot) in BOOKS.items()])
        return [normalize(engine.submit(i[1], to_engine_instruction(i)))
                for i, _, _ in random_stream(9, 2000)]
    assert run() == run()
