"""Seeded random instruction streams for engine and publisher tests.

Instructions are plain tuples (see tests/reference_engine.py). The stream
keeps its own brute-force reference engine up to date, so size and price
changes are always valid, or hit gone handles on purpose.
"""

import random

from b3gen.engine import (
    Cancel, IocOrder, MarketOrder, MassCancel, NewOrder, PriceChange, SizeChange,
)
from reference_engine import ReferenceEngine

TICK = 100   # 0.01 in x10^-4 units
LOT = 100

# name -> (segment, tick, lot). A and B share a segment.
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
