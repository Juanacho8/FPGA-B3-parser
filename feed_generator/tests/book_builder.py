"""Independent order book builder for tests.

Rebuilds each instrument's book from published bytes alone, the way a feed
client would: decodes with tests/sbe_decoder.py and applies only
Order_MBO NEW/CHANGE, DeleteOrder_MBO and MassDeleteOrders_MBO, keyed by
secondaryOrderID. Never imports b3gen. Trades never touch the book.

It also checks what a client can check: a DeleteOrder's price and size match
the stored order, a CHANGE's mDEntryPrevSize matches the stored qty, NEW
never reuses a live soid.
"""

import sbe_decoder

ORDER, DELETE, MASS_DELETE = 50, 51, 52
ACTION_NEW, ACTION_CHANGE, ACTION_DELETE_THRU = 0, 1, 3
SIDE = {"0": "BUY", "1": "SELL"}


class BookBuilder:
    def __init__(self):
        self.books = {}  # securityID -> {soid: {"side", "price", "qty"}}

    def apply(self, data):
        header, f = sbe_decoder.decode(data)
        template = header["templateId"]
        if template not in (ORDER, DELETE, MASS_DELETE):
            return
        book = self.books.setdefault(f["securityID"], {})
        if template == ORDER:
            soid, side = f["secondaryOrderID"], SIDE[f["mDEntryType"]]
            if f["mDUpdateAction"] == ACTION_NEW:
                assert soid not in book, f"NEW reuses live soid {soid}"
                book[soid] = {"side": side, "price": f["mDEntryPx"], "qty": f["mDEntrySize"]}
            elif f["mDUpdateAction"] == ACTION_CHANGE:
                stored = book[soid]
                assert f["mDEntryPrevSize"] == stored["qty"], (soid, f, stored)
                assert (side, f["mDEntryPx"]) == (stored["side"], stored["price"])
                stored["qty"] = f["mDEntrySize"]
            else:
                raise AssertionError(f"unexpected Order_MBO action {f['mDUpdateAction']}")
        elif template == DELETE:
            soid = f["secondaryOrderID"]
            stored = book.pop(soid)
            assert (f["mDEntryPx"], f["mDEntrySize"], SIDE[f["mDEntryType"]]) == \
                (stored["price"], stored["qty"], stored["side"]), (soid, f, stored)
        else:
            assert f["mDUpdateAction"] == ACTION_DELETE_THRU
            side = SIDE[f["mDEntryType"]]
            for soid in [s for s, o in book.items() if o["side"] == side]:
                del book[soid]

    def resting(self, security_id):
        """[(side, price, qty, soid)] in priority order, bids then offers."""
        book = self.books.get(security_id, {})
        bids = sorted((o["price"], s) for s, o in book.items() if o["side"] == "BUY")
        offers = sorted((o["price"], s) for s, o in book.items() if o["side"] == "SELL")
        bids.sort(key=lambda ps: (-ps[0], ps[1]))
        return ([("BUY", p, book[s]["qty"], s) for p, s in bids] +
                [("SELL", p, book[s]["qty"], s) for p, s in offers])
