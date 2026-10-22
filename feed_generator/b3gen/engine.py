"""Layer 2: matching engine.

One Book per instrument. Bids rank price high to low, offers low to high;
within a price, secondaryOrderID ascending (smaller is first in line).
Applies order-flow instructions and returns outcomes: plain dataclasses with
everything the publisher needs, and no B3 message knowledge.

Prices are integer mantissas in the wire's units (x10^-4), quantities are
integers. Each book has a tick size and a lot size.

Identifiers:
* handle: permanent, assigned by the engine when an order enters, unique
  across the engine. The caller refers to the order by it.
* secondaryOrderID (soid): from a counter shared by every book of a market
  segment. Allocated at entry for every new order, even one that fully
  fills (so IDs on the feed have realistic gaps), and again on every
  requeue (size increase or price change).

Rules (docs/FEED_GENERATOR_CONTEXT.md section 5, Engine):
* Size reduction keeps priority and soid. Size increase or price change
  requeues: new soid, new insert time, back of the level.
* A price change that crosses removes the old order, then processes it as
  an aggressive limit order at the new price under its new soid.
* Matching takes the best opposite price first, then priority, until filled
  or no longer crossing. Fills trade at the resting order's price. A limit
  remainder rests at its limit price; a market or IOC remainder is
  discarded. Market orders have no price protection.
* No self-trade prevention.
* Instructions naming an unknown or already-gone handle return Rejected.
  Invalid input (off-tick price, qty not a positive lot multiple, size
  change to 0 or to the same qty, price change to the same price, time going
  backwards) raises EngineError.

Must not: know about B3 messages, rptSeq, or packets.
"""

import bisect
from dataclasses import dataclass, field

BUY = "BUY"
SELL = "SELL"
SIDES = (BUY, SELL)


class EngineError(ValueError):
    """Invalid instruction input."""


class Counter:
    """Increasing integer source. One per market segment for soids, one per
    engine for handles."""

    def __init__(self, start=1):
        self._next = start

    def next(self):
        value = self._next
        self._next += 1
        return value


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NewOrder:
    time_ns: int
    side: str
    price: int
    qty: int
    firm: int


@dataclass(frozen=True)
class MarketOrder:
    time_ns: int
    side: str
    qty: int
    firm: int


@dataclass(frozen=True)
class IocOrder:
    time_ns: int
    side: str
    price: int
    qty: int
    firm: int


@dataclass(frozen=True)
class Cancel:
    time_ns: int
    handle: int


@dataclass(frozen=True)
class SizeChange:
    time_ns: int
    handle: int
    new_qty: int


@dataclass(frozen=True)
class PriceChange:
    time_ns: int
    handle: int
    new_price: int


@dataclass(frozen=True)
class MassCancel:
    time_ns: int
    side: str


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OrderView:
    """A resting order as seen from outside the book."""
    handle: int
    soid: int
    side: str
    price: int
    qty: int
    firm: int
    insert_time_ns: int


@dataclass(frozen=True)
class Rested:
    instrument: str
    time_ns: int
    handle: int
    soid: int
    side: str
    price: int
    qty: int
    firm: int
    insert_time_ns: int


@dataclass(frozen=True)
class Fill:
    resting_handle: int
    resting_soid: int
    resting_firm: int
    resting_insert_time_ns: int
    price: int               # the resting order's price
    qty: int
    resting_qty_before: int
    resting_qty_after: int   # 0 means the resting order is gone


@dataclass(frozen=True)
class Executed:
    instrument: str
    time_ns: int
    order_type: str          # "limit", "ioc" or "market"
    handle: int
    soid: int                # the aggressor's soid (allocated at entry/requeue)
    side: str
    price: int | None        # limit price; None for market
    qty: int                 # aggressor qty when it started matching
    firm: int
    fills: tuple
    filled_qty: int
    remainder: Rested | None  # limit remainder that rests
    discarded_qty: int        # market/IOC remainder thrown away
    replaced: OrderView | None = None  # crossing price change: the order removed first


@dataclass(frozen=True)
class Unfilled:
    """Market or IOC order that found nothing to trade against."""
    instrument: str
    time_ns: int
    order_type: str
    handle: int
    soid: int
    side: str
    price: int | None
    qty: int
    firm: int
    discarded_qty: int


@dataclass(frozen=True)
class Cancelled:
    instrument: str
    time_ns: int
    order: OrderView


@dataclass(frozen=True)
class SizeReduced:
    instrument: str
    time_ns: int
    handle: int
    soid: int
    side: str
    price: int
    old_qty: int
    new_qty: int
    firm: int
    insert_time_ns: int


@dataclass(frozen=True)
class Requeued:
    instrument: str
    time_ns: int
    handle: int
    side: str
    firm: int
    old_soid: int
    new_soid: int
    old_price: int
    new_price: int
    old_qty: int
    new_qty: int
    old_insert_time_ns: int
    new_insert_time_ns: int


@dataclass(frozen=True)
class MassCancelled:
    instrument: str
    time_ns: int
    side: str
    orders: tuple            # OrderViews removed, in priority order


@dataclass(frozen=True)
class Rejected:
    instrument: str
    time_ns: int
    instruction: str
    handle: int
    reason: str


# ---------------------------------------------------------------------------
# Book
# ---------------------------------------------------------------------------

@dataclass
class _Order:
    handle: int
    soid: int
    side: str
    price: int
    qty: int
    firm: int
    insert_time_ns: int

    def view(self):
        return OrderView(self.handle, self.soid, self.side, self.price, self.qty,
                         self.firm, self.insert_time_ns)


@dataclass
class _Side:
    """One side of a book: price -> level (handle -> order, in soid order)
    and a sorted list of prices. best() is the first price in priority."""
    side: str
    levels: dict = field(default_factory=dict)
    prices: list = field(default_factory=list)  # ascending
    level_qty: dict = field(default_factory=dict)  # price -> total qty, kept up to date

    def best(self):
        if not self.prices:
            return None
        return self.prices[-1] if self.side == BUY else self.prices[0]

    def prices_in_priority(self):
        return list(reversed(self.prices)) if self.side == BUY else list(self.prices)

    def add(self, order):
        level = self.levels.get(order.price)
        if level is None:
            level = self.levels[order.price] = {}
            self.level_qty[order.price] = 0
            bisect.insort(self.prices, order.price)
        # New and requeued orders always carry the newest soid, so
        # appending keeps the level in soid order.
        level[order.handle] = order
        self.level_qty[order.price] += order.qty

    def remove(self, order):
        level = self.levels[order.price]
        del level[order.handle]
        self.level_qty[order.price] -= order.qty
        if not level:
            del self.levels[order.price]
            del self.level_qty[order.price]
            self.prices.pop(bisect.bisect_left(self.prices, order.price))

    def change_qty(self, order, new_qty):
        """Change a resting order's qty in place (keeps its priority)."""
        self.level_qty[order.price] += new_qty - order.qty
        order.qty = new_qty


class Book:
    def __init__(self, instrument, tick, lot, soid_counter, handle_counter):
        if tick <= 0 or lot <= 0:
            raise EngineError("tick and lot must be positive")
        self.instrument = instrument
        self.tick = tick
        self.lot = lot
        self._soids = soid_counter
        self._handles = handle_counter
        self._sides = {BUY: _Side(BUY), SELL: _Side(SELL)}
        self._orders = {}       # handle -> resting _Order
        self._last_time_ns = None

    # -- public -------------------------------------------------------------

    def apply(self, instruction):
        self._check_time(instruction.time_ns)
        kind = type(instruction)
        if kind is NewOrder:
            return self._enter("limit", instruction.time_ns, instruction.side,
                               instruction.price, instruction.qty, instruction.firm)
        if kind is IocOrder:
            return self._enter("ioc", instruction.time_ns, instruction.side,
                               instruction.price, instruction.qty, instruction.firm)
        if kind is MarketOrder:
            return self._enter("market", instruction.time_ns, instruction.side,
                               None, instruction.qty, instruction.firm)
        if kind is Cancel:
            return self._cancel(instruction)
        if kind is SizeChange:
            return self._size_change(instruction)
        if kind is PriceChange:
            return self._price_change(instruction)
        if kind is MassCancel:
            return self._mass_cancel(instruction)
        raise EngineError(f"unknown instruction {instruction!r}")

    def top(self, n):
        """(bids, offers): up to n levels per side, best first, each
        (price, total qty, order count)."""
        result = []
        for side in SIDES:
            s = self._sides[side]
            levels = []
            best_n = s.prices[:-n - 1:-1] if side == BUY else s.prices[:n]
            for price in best_n:
                levels.append((price, s.level_qty[price], len(s.levels[price])))
            result.append(levels)
        return tuple(result)

    def resting_orders(self):
        """Every resting order in priority order, bids then offers."""
        views = []
        for side in SIDES:
            s = self._sides[side]
            for price in s.prices_in_priority():
                views.extend(o.view() for o in s.levels[price].values())
        return tuple(views)

    def check_invariants(self):
        """Raise AssertionError if the book is inconsistent."""
        seen_handles = set()
        seen_soids = set()
        for side in SIDES:
            s = self._sides[side]
            assert s.prices == sorted(s.prices), "price list not sorted"
            assert len(set(s.prices)) == len(s.prices), "duplicate price"
            assert set(s.prices) == set(s.levels), "price list and levels disagree"
            assert set(s.level_qty) == set(s.levels), "level totals and levels disagree"
            for price, level in s.levels.items():
                assert level, f"empty level {price}"
                assert s.level_qty[price] == sum(o.qty for o in level.values()), \
                    f"level total out of sync at {price}"
                assert price % self.tick == 0, f"off-tick price {price}"
                soids = [o.soid for o in level.values()]
                assert soids == sorted(soids), f"priority broken at {price}"
                for handle, o in level.items():
                    assert o.handle == handle and o.side == side and o.price == price
                    assert o.qty > 0 and o.qty % self.lot == 0, f"bad qty {o.qty}"
                    assert self._orders.get(handle) is o, "handle index out of sync"
                    assert o.soid not in seen_soids, f"duplicate soid {o.soid}"
                    seen_handles.add(handle)
                    seen_soids.add(o.soid)
        assert seen_handles == set(self._orders), "handle index has extra orders"
        bid, ask = self._sides[BUY].best(), self._sides[SELL].best()
        assert bid is None or ask is None or bid < ask, f"crossed book {bid} >= {ask}"

    # -- validation ---------------------------------------------------------

    def _check_time(self, time_ns):
        if self._last_time_ns is not None and time_ns < self._last_time_ns:
            raise EngineError(f"{self.instrument}: time {time_ns} before {self._last_time_ns}")
        self._last_time_ns = time_ns

    def _check_side(self, side):
        if side not in SIDES:
            raise EngineError(f"bad side {side!r}")

    def _check_price(self, price):
        if type(price) is not int or price <= 0 or price % self.tick:
            raise EngineError(f"{self.instrument}: price {price!r} is not a positive "
                              f"multiple of tick {self.tick}")

    def _check_qty(self, qty):
        if type(qty) is not int or qty <= 0 or qty % self.lot:
            raise EngineError(f"{self.instrument}: qty {qty!r} is not a positive "
                              f"multiple of lot {self.lot}")

    def _rejected(self, instruction, reason):
        return Rejected(self.instrument, instruction.time_ns, type(instruction).__name__,
                        instruction.handle, reason)

    # -- instructions -------------------------------------------------------

    def _enter(self, order_type, time_ns, side, price, qty, firm):
        self._check_side(side)
        if order_type != "market":
            self._check_price(price)
        self._check_qty(qty)
        handle = self._handles.next()
        soid = self._soids.next()  # at entry, even if it fully fills
        return self._match(order_type, time_ns, handle, soid, side, price, qty, firm,
                           insert_time_ns=time_ns, replaced=None)

    def _match(self, order_type, time_ns, handle, soid, side, price, qty, firm,
               insert_time_ns, replaced):
        """Match an aggressor, then rest, discard or report Unfilled."""
        opposite = self._sides[SELL if side == BUY else BUY]
        remaining = qty
        fills = []
        while remaining:
            best = opposite.best()
            if best is None:
                break
            if price is not None and (best > price if side == BUY else best < price):
                break
            level = opposite.levels[best]
            resting = next(iter(level.values()))  # first in priority
            traded = min(remaining, resting.qty)
            before = resting.qty
            opposite.change_qty(resting, resting.qty - traded)
            remaining -= traded
            fills.append(Fill(resting.handle, resting.soid, resting.firm,
                              resting.insert_time_ns, best, traded, before, resting.qty))
            if resting.qty == 0:
                opposite.remove(resting)
                del self._orders[resting.handle]

        remainder = None
        discarded = 0
        if remaining and order_type == "limit":
            order = _Order(handle, soid, side, price, remaining, firm, insert_time_ns)
            self._rest(order)
            remainder = Rested(self.instrument, time_ns, handle, soid, side, price,
                               remaining, firm, insert_time_ns)
        elif remaining:
            discarded = remaining

        if not fills:
            if remainder is not None:
                return remainder
            return Unfilled(self.instrument, time_ns, order_type, handle, soid, side,
                            price, qty, firm, discarded)
        return Executed(self.instrument, time_ns, order_type, handle, soid, side, price,
                        qty, firm, tuple(fills), qty - remaining, remainder, discarded,
                        replaced)

    def _rest(self, order):
        self._sides[order.side].add(order)
        self._orders[order.handle] = order

    def _unrest(self, order):
        self._sides[order.side].remove(order)
        del self._orders[order.handle]

    def _cancel(self, instruction):
        order = self._orders.get(instruction.handle)
        if order is None:
            return self._rejected(instruction, "unknown or gone handle")
        self._unrest(order)
        return Cancelled(self.instrument, instruction.time_ns, order.view())

    def _size_change(self, instruction):
        new_qty = instruction.new_qty
        self._check_qty(new_qty)
        order = self._orders.get(instruction.handle)
        if order is None:
            return self._rejected(instruction, "unknown or gone handle")
        if new_qty == order.qty:
            raise EngineError(f"{self.instrument}: size change to the same qty {new_qty}")
        if new_qty < order.qty:
            # Reduction keeps priority, soid and insert time.
            old_qty = order.qty
            self._sides[order.side].change_qty(order, new_qty)
            return SizeReduced(self.instrument, instruction.time_ns, order.handle,
                               order.soid, order.side, order.price, old_qty, new_qty,
                               order.firm, order.insert_time_ns)
        return self._requeue(order, instruction.time_ns, order.price, new_qty)

    def _price_change(self, instruction):
        new_price = instruction.new_price
        self._check_price(new_price)
        order = self._orders.get(instruction.handle)
        if order is None:
            return self._rejected(instruction, "unknown or gone handle")
        if new_price == order.price:
            raise EngineError(f"{self.instrument}: price change to the same price {new_price}")
        opposite_best = self._sides[SELL if order.side == BUY else BUY].best()
        crosses = opposite_best is not None and (
            opposite_best <= new_price if order.side == BUY else opposite_best >= new_price)
        if not crosses:
            return self._requeue(order, instruction.time_ns, new_price, order.qty)
        # Crossing: remove the old order, then aggress at the new price under
        # a new soid. A remainder rests with that soid and the new insert time.
        replaced = order.view()
        self._unrest(order)
        return self._match("limit", instruction.time_ns, order.handle, self._soids.next(),
                           order.side, new_price, order.qty, order.firm,
                           insert_time_ns=instruction.time_ns, replaced=replaced)

    def _requeue(self, order, time_ns, new_price, new_qty):
        """Loses priority: new soid, new insert time, back of the level."""
        old = order.view()
        self._unrest(order)
        order.soid = self._soids.next()
        order.price = new_price
        order.qty = new_qty
        order.insert_time_ns = time_ns
        self._rest(order)
        return Requeued(self.instrument, time_ns, order.handle, order.side, order.firm,
                        old.soid, order.soid, old.price, new_price, old.qty, new_qty,
                        old.insert_time_ns, time_ns)

    def _mass_cancel(self, instruction):
        self._check_side(instruction.side)
        s = self._sides[instruction.side]
        removed = []
        for price in s.prices_in_priority():
            removed.extend(s.levels[price].values())
        for order in removed:
            self._unrest(order)
        return MassCancelled(self.instrument, instruction.time_ns, instruction.side,
                             tuple(o.view() for o in removed))


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    """Books for several instruments. Books of the same market segment
    share one soid counter; handles are unique across the engine."""

    def __init__(self, instruments, *, soid_start=1, handle_start=1):
        """instruments: iterable of (name, tick, lot, segment)."""
        self._handles = Counter(handle_start)
        self.segments = {}
        self.books = {}
        for name, tick, lot, segment in instruments:
            if name in self.books:
                raise EngineError(f"duplicate instrument {name}")
            counter = self.segments.setdefault(segment, Counter(soid_start))
            self.books[name] = Book(name, tick, lot, counter, self._handles)

    def submit(self, instrument, instruction):
        book = self.books.get(instrument)
        if book is None:
            raise EngineError(f"unknown instrument {instrument}")
        return book.apply(instruction)
