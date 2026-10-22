"""Brute-force reference matching engine for tests. Never imports b3gen.

Deliberately naive: every resting order of every book lives in one flat
list, and the book is re-sorted from scratch whenever it is needed. It takes
instructions as plain tuples and returns outcomes as (kind, dict) in the
same shape as dataclasses.asdict() of b3gen.engine's outcomes, so the two
can be compared directly.

Instruction tuples:
    ("new", book, time, side, price, qty, firm)
    ("ioc", book, time, side, price, qty, firm)
    ("market", book, time, side, qty, firm)
    ("cancel", book, time, handle)
    ("size", book, time, handle, new_qty)
    ("price", book, time, handle, new_price)
    ("mass", book, time, side)
"""


class ReferenceEngine:
    def __init__(self, books):
        """books: {name: segment}. Books in one segment share soids."""
        self.segment_of = dict(books)
        self.next_soid = {seg: 1 for seg in self.segment_of.values()}
        self.next_handle = 1
        self.orders = []  # dicts: book, handle, soid, side, price, qty, firm, insert

    # -- helpers ------------------------------------------------------------

    def _soid(self, book):
        seg = self.segment_of[book]
        value = self.next_soid[seg]
        self.next_soid[seg] += 1
        return value

    def _sorted(self, book, side):
        """Orders of one side in priority order, recomputed every time."""
        mine = [o for o in self.orders if o["book"] == book and o["side"] == side]
        if side == "BUY":
            return sorted(mine, key=lambda o: (-o["price"], o["soid"]))
        return sorted(mine, key=lambda o: (o["price"], o["soid"]))

    def _find(self, book, handle):
        for o in self.orders:
            if o["book"] == book and o["handle"] == handle:
                return o
        return None

    @staticmethod
    def _view(o):
        return {"handle": o["handle"], "soid": o["soid"], "side": o["side"],
                "price": o["price"], "qty": o["qty"], "firm": o["firm"],
                "insert_time_ns": o["insert"]}

    # -- queries ------------------------------------------------------------

    def top(self, book, n):
        result = []
        for side in ("BUY", "SELL"):
            levels = []
            for o in self._sorted(book, side):
                if levels and levels[-1][0] == o["price"]:
                    p, q, c = levels[-1]
                    levels[-1] = (p, q + o["qty"], c + 1)
                else:
                    levels.append((o["price"], o["qty"], 1))
            result.append(levels[:n])
        return tuple(result)

    def resting_orders(self, book):
        return [self._view(o) for side in ("BUY", "SELL") for o in self._sorted(book, side)]

    def order(self, book, handle):
        o = self._find(book, handle)
        return None if o is None else dict(o)

    # -- instructions -------------------------------------------------------

    def apply(self, instr):
        kind, book, time = instr[0], instr[1], instr[2]
        if kind in ("new", "ioc"):
            _, _, _, side, price, qty, firm = instr
            return self._enter(book, time, {"new": "limit", "ioc": "ioc"}[kind],
                               side, price, qty, firm)
        if kind == "market":
            _, _, _, side, qty, firm = instr
            return self._enter(book, time, "market", side, None, qty, firm)
        if kind == "mass":
            side = instr[3]
            removed = self._sorted(book, side)
            for o in removed:
                self.orders.remove(o)
            return "MassCancelled", {"instrument": book, "time_ns": time, "side": side,
                                     "orders": tuple(self._view(o) for o in removed)}

        handle = instr[3]
        o = self._find(book, handle)
        names = {"cancel": "Cancel", "size": "SizeChange", "price": "PriceChange"}
        if o is None:
            return "Rejected", {"instrument": book, "time_ns": time,
                                "instruction": names[kind], "handle": handle,
                                "reason": "unknown or gone handle"}
        if kind == "cancel":
            self.orders.remove(o)
            return "Cancelled", {"instrument": book, "time_ns": time, "order": self._view(o)}
        if kind == "size":
            new_qty = instr[4]
            if new_qty < o["qty"]:
                old = o["qty"]
                o["qty"] = new_qty
                return "SizeReduced", {
                    "instrument": book, "time_ns": time, "handle": handle, "soid": o["soid"],
                    "side": o["side"], "price": o["price"], "old_qty": old, "new_qty": new_qty,
                    "firm": o["firm"], "insert_time_ns": o["insert"]}
            return self._requeue(book, time, o, o["price"], new_qty)
        # price change
        new_price = instr[4]
        opposite = self._sorted(book, "SELL" if o["side"] == "BUY" else "BUY")
        crosses = bool(opposite) and (
            opposite[0]["price"] <= new_price if o["side"] == "BUY"
            else opposite[0]["price"] >= new_price)
        if not crosses:
            return self._requeue(book, time, o, new_price, o["qty"])
        replaced = self._view(o)
        self.orders.remove(o)
        return self._match(book, time, "limit", handle, self._soid(book), o["side"],
                           new_price, o["qty"], o["firm"], replaced)

    def _requeue(self, book, time, o, new_price, new_qty):
        old = dict(o)
        o.update(soid=self._soid(book), price=new_price, qty=new_qty, insert=time)
        return "Requeued", {
            "instrument": book, "time_ns": time, "handle": o["handle"], "side": o["side"],
            "firm": o["firm"], "old_soid": old["soid"], "new_soid": o["soid"],
            "old_price": old["price"], "new_price": new_price, "old_qty": old["qty"],
            "new_qty": new_qty, "old_insert_time_ns": old["insert"],
            "new_insert_time_ns": time}

    def _enter(self, book, time, order_type, side, price, qty, firm):
        handle = self.next_handle
        self.next_handle += 1
        return self._match(book, time, order_type, handle, self._soid(book), side, price,
                           qty, firm, None)

    def _match(self, book, time, order_type, handle, soid, side, price, qty, firm, replaced):
        remaining = qty
        fills = []
        while remaining:
            opposite = self._sorted(book, "SELL" if side == "BUY" else "BUY")
            if not opposite:
                break
            best = opposite[0]
            if price is not None and (best["price"] > price if side == "BUY"
                                      else best["price"] < price):
                break
            traded = min(remaining, best["qty"])
            before = best["qty"]
            best["qty"] -= traded
            remaining -= traded
            fills.append({"resting_handle": best["handle"], "resting_soid": best["soid"],
                          "resting_firm": best["firm"],
                          "resting_insert_time_ns": best["insert"], "price": best["price"],
                          "qty": traded, "resting_qty_before": before,
                          "resting_qty_after": best["qty"]})
            if best["qty"] == 0:
                self.orders.remove(best)

        remainder = None
        discarded = 0
        if remaining and order_type == "limit":
            self.orders.append({"book": book, "handle": handle, "soid": soid, "side": side,
                                "price": price, "qty": remaining, "firm": firm,
                                "insert": time})
            remainder = {"instrument": book, "time_ns": time, "handle": handle,
                         "soid": soid, "side": side, "price": price, "qty": remaining,
                         "firm": firm, "insert_time_ns": time}
        elif remaining:
            discarded = remaining

        if not fills:
            if remainder is not None:
                return "Rested", remainder
            return "Unfilled", {"instrument": book, "time_ns": time, "order_type": order_type,
                                "handle": handle, "soid": soid, "side": side, "price": price,
                                "qty": qty, "firm": firm, "discarded_qty": discarded}
        return "Executed", {
            "instrument": book, "time_ns": time, "order_type": order_type, "handle": handle,
            "soid": soid, "side": side, "price": price, "qty": qty, "firm": firm,
            "fills": tuple(fills), "filled_qty": qty - remaining, "remainder": remainder,
            "discarded_qty": discarded, "replaced": replaced}
