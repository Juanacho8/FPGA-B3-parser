"""Layer 1: order flow.

A zero-intelligence model per instrument: limit orders a few ticks around
mid (some crossing the spread), cancels, size changes and price changes of
its own live orders, and occasional small market and IOC orders. Sizes are
lot multiples, prices stay inside the session's PriceBand.

Timing is bursty: each instrument's arrivals are a Poisson process whose
rate depends on a market-wide regime (calm or busy). Regime switches come
from their own RNG, random.Random(f"{seed}:regime"), and nothing else uses
it; each instrument has its own random.Random(f"{seed}:{name}") for its
arrivals and decisions, and its own rate in each regime.

The flow only sees what a trader would: top(5) of the instrument's book,
the orders it created and the outcomes telling it what happened to them.

Must not: know about the book's internals or B3 messages.
See docs/FEED_GENERATOR_CONTEXT.md section 8.
"""

import heapq
import random
from dataclasses import dataclass

from b3gen import engine

CALM = "calm"
BUSY = "busy"
SECOND = 1_000_000_000


@dataclass(frozen=True)
class FlowParams:
    rates: dict                       # instrument -> (calm rate, busy rate), arrivals/s
    calm_mean_ns: int = 2 * SECOND    # mean regime durations
    busy_mean_ns: int = SECOND // 2
    # Instruction mix (normalized).
    p_new: float = 0.55
    p_cancel: float = 0.18            # at target_live_orders; scales with live orders
    target_live_orders: int = 200     # per instrument
    p_size: float = 0.08
    p_price: float = 0.09
    p_ioc: float = 0.06
    p_market: float = 0.04
    near_ticks: int = 5               # passive orders 0..near_ticks-1 ticks from mid
    cross_fraction: float = 0.10      # share of new limits that cross the spread
    cross_ticks: int = 2              # how far past the best opposite price
    lots_continue: float = 0.6        # geometric size: P(one more lot)
    max_lots: int = 20
    aggressive_max_lots: int = 3      # market and IOC sizes stay small
    price_change_ticks: int = 4
    thin_levels: int = 3              # a side with fewer levels is "thin"
    thin_bias: float = 0.8            # share of new limits sent to a thin side
    firms: tuple = tuple(range(1, 31))


@dataclass(frozen=True)
class FlowInstrument:
    name: str
    tick: int
    lot: int
    low_limit: int     # PriceBand
    high_limit: int
    reference_price: int  # previous close: mid when the book and tape are empty


@dataclass(frozen=True)
class Arrival:
    time_ns: int
    instrument: str
    regime: str


class RegimeClock:
    """Market-wide calm/busy regimes. Switch times come only from this
    clock's RNG, generated lazily in time order, so queries in any order
    see the same timeline."""

    def __init__(self, seed, start_ns, calm_mean_ns, busy_mean_ns):
        self._rng = random.Random(f"{seed}:regime")
        self._means = {CALM: calm_mean_ns, BUSY: busy_mean_ns}
        self.segments = []  # (start_ns, end_ns, regime), contiguous
        self._extend(start_ns, CALM)

    def _extend(self, start_ns, regime):
        duration = max(1, round(self._rng.expovariate(1 / self._means[regime])))
        self.segments.append((start_ns, start_ns + duration, regime))

    def at(self, t):
        """(regime, segment end) for time t."""
        while self.segments[-1][1] <= t:
            _, end, regime = self.segments[-1]
            self._extend(end, BUSY if regime == CALM else CALM)
        lo, hi = 0, len(self.segments) - 1
        while lo < hi:  # last segment starting at or before t
            mid = (lo + hi + 1) // 2
            if self.segments[mid][0] <= t:
                lo = mid
            else:
                hi = mid - 1
        _, end, regime = self.segments[lo]
        return regime, end


class _LiveOrders:
    """The instrument's own resting orders, with O(1) random choice."""

    def __init__(self):
        self._handles = []
        self._pos = {}
        self.orders = {}  # handle -> [side, price, qty]

    def __len__(self):
        return len(self._handles)

    def add(self, handle, side, price, qty):
        self._pos[handle] = len(self._handles)
        self._handles.append(handle)
        self.orders[handle] = [side, price, qty]

    def remove(self, handle):
        i = self._pos.pop(handle)
        last = self._handles.pop()
        if last != handle:
            self._handles[i] = last
            self._pos[last] = i
        del self.orders[handle]

    def choice(self, rng):
        return self._handles[rng.randrange(len(self._handles))]


class _InstrumentFlow:
    def __init__(self, inst, rates, params, seed, clock, start_ns):
        self.inst = inst
        self.rates = {CALM: rates[0], BUSY: rates[1]}
        self.p = params
        self.rng = random.Random(f"{seed}:{inst.name}")
        self.clock = clock
        self.live = _LiveOrders()
        self.last_trade = None
        self.low = -(-inst.low_limit // inst.tick) * inst.tick   # band, on tick
        self.high = inst.high_limit // inst.tick * inst.tick
        if self.low > self.high:
            raise ValueError(f"{inst.name}: band holds no tick")
        self.next_time = self._draw_after(start_ns)

    # -- timing ---------------------------------------------------------------

    def _draw_after(self, t):
        """Next arrival of a Poisson process with a piecewise-constant rate:
        draw at the current regime's rate; if the draw passes the regime's end,
        restart from that boundary (memoryless) at the next regime's rate."""
        while True:
            regime, end = self.clock.at(t)
            rate = self.rates[regime]
            if rate > 0:
                arrival = t + max(1, round(self.rng.expovariate(rate) * SECOND))
                if arrival < end:
                    return arrival
            t = end

    def advance(self):
        t = self.next_time
        self.next_time = self._draw_after(t)
        return t

    # -- decisions ------------------------------------------------------------

    def _clamp(self, price):
        return min(self.high, max(self.low, price))

    def _mid(self, bids, offers):
        tick = self.inst.tick
        if bids and offers:
            total = bids[0][0] + offers[0][0]
            mid = total // (2 * tick) * tick
            if total % (2 * tick):
                # Between two ticks: round up or down at random, so prices
                # don't drift one way.
                mid += tick * self.rng.randint(0, 1)
        elif bids:
            mid = bids[0][0] + tick
        elif offers:
            mid = offers[0][0] - tick
        else:
            base = self.last_trade if self.last_trade is not None else self.inst.reference_price
            mid = round(base / tick) * tick
        return self._clamp(mid)

    def _lots(self, cap):
        lots = 1
        while lots < cap and self.rng.random() < self.p.lots_continue:
            lots += 1
        return lots * self.inst.lot

    def _firm(self):
        return self.p.firms[self.rng.randrange(len(self.p.firms))]

    def decide(self, t, top):
        """One instruction for time t, given top(5) = (bids, offers)."""
        bids, offers = top
        kind = self._kind()
        if kind in ("cancel", "size", "price") and not self.live:
            kind = "new"
        if kind == "size":
            instruction = self._size_change(t)
        elif kind == "price":
            instruction = self._price_change(t, bids, offers)
        else:
            instruction = None
        if instruction is not None:
            return instruction
        if kind == "cancel":
            return engine.Cancel(t, self.live.choice(self.rng))
        if kind == "ioc":
            return self._ioc(t, bids, offers)
        if kind == "market":
            side = engine.BUY if self.rng.random() < 0.5 else engine.SELL
            return engine.MarketOrder(t, side, self._lots(self.p.aggressive_max_lots), self._firm())
        return self._new_limit(t, bids, offers)

    def _kind(self):
        """Pick an instruction kind. The cancel weight grows with the number
        of live orders (each order has a roughly constant chance of being
        cancelled), so the book settles around target_live_orders instead of
        growing for ever."""
        p = self.p
        weights = (("new", p.p_new),
                   ("cancel", p.p_cancel * len(self.live) / p.target_live_orders),
                   ("size", p.p_size), ("price", p.p_price),
                   ("ioc", p.p_ioc), ("market", p.p_market))
        r = self.rng.random() * sum(w for _, w in weights)
        for kind, w in weights:
            if r < w:
                return kind
            r -= w
        return weights[-1][0]

    def _new_limit(self, t, bids, offers):
        tick = self.inst.tick
        thin_bid = len(bids) < self.p.thin_levels
        thin_offer = len(offers) < self.p.thin_levels
        if thin_bid != thin_offer and self.rng.random() < self.p.thin_bias:
            side = engine.BUY if thin_bid else engine.SELL
        else:
            side = engine.BUY if self.rng.random() < 0.5 else engine.SELL
        opposite = offers if side == engine.BUY else bids
        if opposite and self.rng.random() < self.p.cross_fraction:
            k = self.rng.randint(0, self.p.cross_ticks)
            price = opposite[0][0] + (k * tick if side == engine.BUY else -k * tick)
        else:
            mid = self._mid(bids, offers)
            k = self.rng.randrange(self.p.near_ticks)
            if side == engine.BUY:
                price = mid - k * tick
                if offers:
                    price = min(price, offers[0][0] - tick)  # stay passive
            else:
                price = mid + k * tick
                if bids:
                    price = max(price, bids[0][0] + tick)
        return engine.NewOrder(t, side, self._clamp(price), self._lots(self.p.max_lots), self._firm())

    def _ioc(self, t, bids, offers):
        side = engine.BUY if self.rng.random() < 0.5 else engine.SELL
        opposite = offers if side == engine.BUY else bids
        if opposite:
            k = self.rng.randint(0, 1)
            price = opposite[0][0] + (k * self.inst.tick if side == engine.BUY else -k * self.inst.tick)
        else:
            price = self._mid(bids, offers)
        return engine.IocOrder(t, side, self._clamp(price),
                               self._lots(self.p.aggressive_max_lots), self._firm())

    def _size_change(self, t):
        handle = self.live.choice(self.rng)
        current = self.live.orders[handle][2] // self.inst.lot
        # Half reductions (keep priority), half increases (requeue).
        if current > 1 and self.rng.random() < 0.5:
            lots = self.rng.randint(1, current - 1)
        else:
            lots = current + self.rng.randint(1, max(1, self.p.max_lots // 4))
        return engine.SizeChange(t, handle, lots * self.inst.lot)

    def _price_change(self, t, bids, offers):
        handle = self.live.choice(self.rng)
        price = self.live.orders[handle][1]
        k = self.rng.randint(1, self.p.price_change_ticks) * self.rng.choice((-1, 1))
        new_price = self._clamp(price + k * self.inst.tick)
        if new_price == price:
            new_price = self._clamp(price - k * self.inst.tick)
        if new_price == price:
            return None
        return engine.PriceChange(t, handle, new_price)

    # -- feedback -------------------------------------------------------------

    def on_outcome(self, outcome):
        kind = type(outcome)
        if kind is engine.Rested:
            self.live.add(outcome.handle, outcome.side, outcome.price, outcome.qty)
        elif kind is engine.Executed:
            if outcome.replaced is not None:
                self.live.remove(outcome.handle)
            for fill in outcome.fills:
                if fill.resting_qty_after == 0:
                    self.live.remove(fill.resting_handle)
                else:
                    self.live.orders[fill.resting_handle][2] = fill.resting_qty_after
            self.last_trade = outcome.fills[-1].price
            if outcome.remainder is not None:
                r = outcome.remainder
                self.live.add(r.handle, r.side, r.price, r.qty)
        elif kind is engine.Cancelled:
            self.live.remove(outcome.order.handle)
        elif kind is engine.SizeReduced:
            self.live.orders[outcome.handle][2] = outcome.new_qty
        elif kind is engine.Requeued:
            self.live.orders[outcome.handle][1:] = [outcome.new_price, outcome.new_qty]
        elif kind is engine.MassCancelled:
            for o in outcome.orders:
                self.live.remove(o.handle)
        elif kind is engine.Rejected:
            raise RuntimeError(f"order flow sent an instruction that was rejected: {outcome}")


class OrderFlow:
    """All instruments' order flow, merged in time order."""

    def __init__(self, instruments, params, seed, start_ns):
        missing = {i.name for i in instruments} - set(params.rates)
        if missing:
            raise ValueError(f"no arrival rates for {sorted(missing)}")
        self.clock = RegimeClock(seed, start_ns, params.calm_mean_ns, params.busy_mean_ns)
        self.flows = {i.name: _InstrumentFlow(i, params.rates[i.name], params, seed,
                                              self.clock, start_ns) for i in instruments}
        self._order = {i.name: n for n, i in enumerate(instruments)}
        self._heap = [(f.next_time, self._order[name], name) for name, f in self.flows.items()]
        heapq.heapify(self._heap)

    def next_arrival(self):
        """The next (time_ns, instrument) across all instruments."""
        t, order, name = heapq.heappop(self._heap)
        flow = self.flows[name]
        assert flow.advance() == t
        heapq.heappush(self._heap, (flow.next_time, order, name))
        return Arrival(t, name, self.clock.at(t)[0])

    def decide(self, arrival, top):
        return self.flows[arrival.instrument].decide(arrival.time_ns, top)

    def on_outcome(self, outcome):
        self.flows[outcome.instrument].on_outcome(outcome)

