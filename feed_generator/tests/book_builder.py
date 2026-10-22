"""Independent order book builder for tests.

Rebuilds each instrument's book from published bytes alone, the way a feed
client would: decodes with tests/sbe_decoder.py and applies only
Order_MBO NEW/CHANGE, DeleteOrder_MBO and MassDeleteOrders_MBO, keyed by
secondaryOrderID. Never imports b3gen. Trades never touch the book.

It also checks what a client can check: a DeleteOrder's price and size match
the stored order, a CHANGE's mDEntryPrevSize matches the stored qty, NEW
never reuses a live soid.

apply_packet() adds the client-side sequencing and reset rules
(spec/LAYOUTS.md sections 12.1, 12.3, 12.5, 12.9):
* sequenceNumber +1 per packet within a sequenceVersion; heartbeats
  (sequenceNumber 0) only checked for nextSeqNo.
* A packet holding SequenceReset is accepted without checking its own
  sequenceNumber or version (B3 calls them arbitrary, Guidelines p.45). The
  next packet must be sequenceVersion +1 (1 after 65534), sequenceNumber 1.
* ChannelReset clears every book on its channel; EmptyBook clears one.
* rptSeq is previous + 1 per instrument, or 1 on the message after an
  EmptyBook.
* Unknown templates are skipped; a datagram too short for a packet header is
  discarded. Callers pass only the complete messages before a packet fault
  (tests/wire.py split_messages).
"""

import sbe_decoder

ORDER, DELETE, MASS_DELETE = 50, 51, 52
SEQUENCE_RESET, SEQUENCE, EMPTY_BOOK, CHANNEL_RESET = 1, 2, 9, 11
MAX_VERSION = 65534
ACTION_NEW, ACTION_CHANGE, ACTION_DELETE_THRU = 0, 1, 3
SIDE = {"0": "BUY", "1": "SELL"}


class BookBuilder:
    def __init__(self):
        self.books = {}     # securityID -> {soid: {"side", "price", "qty"}}
        self.channels = {}  # channel -> {"version", "next", "reset", "sids"}
        self.rpt_seq = {}   # securityID -> last rptSeq (0 after EmptyBook)

    def apply_packet(self, channel, version, seq, messages, after=None, before=None,
                     skip=None):
        """One packet with the client's sequencing and reset rules.
        before(index) / after(index) run around each message (checkpoints
        can fall mid-packet); a message for which skip(index, data) is true
        is not applied (a book the caller no longer trusts)."""
        if version is None:
            return  # a datagram too short for a packet header: discarded
        ch = self.channels.setdefault(channel, {"version": None, "next": None,
                                                "reset": False, "sids": set()})
        templates = [sbe_decoder.decode(m)[0]["templateId"] for m in messages]
        if seq == 0:
            assert templates == [SEQUENCE], (channel, templates)
            expected = 1 if ch["reset"] else ch["next"]
            if expected is not None:
                assert sbe_decoder.decode(messages[0])[1]["nextSeqNo"] == expected, channel
            return
        if SEQUENCE_RESET in templates:
            ch["reset"] = True  # its own seq and version are not checked
        else:
            if ch["reset"]:
                want = 1 if ch["version"] >= MAX_VERSION else ch["version"] + 1
                assert (version, seq) == (want, 1), (channel, version, seq, "after SequenceReset")
            elif ch["version"] is not None:
                assert (version, seq) == (ch["version"], ch["next"]), (channel, version, seq, ch)
            ch.update(version=version, next=seq + 1, reset=False)
        for index, data in enumerate(messages):
            if before:
                before(index)
            if not (skip and skip(index, data)):
                self.apply(data, channel)
            if after:
                after(index)

    def apply(self, data, channel=None):
        """One message. With a channel (apply_packet), also the reset and
        rptSeq rules."""
        header, f = sbe_decoder.decode(data)
        if f is None:
            return  # unknown template
        template = header["templateId"]
        if channel is not None:
            self._client_rules(template, f, channel)
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

    def _client_rules(self, template, f, channel):
        sids = self.channels[channel]["sids"]
        if template == CHANNEL_RESET:
            for sid in sids:
                self.books[sid] = {}
            return
        sid = f.get("securityID")
        if sid is None:
            return
        sids.add(sid)
        if template == EMPTY_BOOK:
            self.books[sid] = {}
            self.rpt_seq[sid] = 0
        elif "rptSeq" in f:
            last = self.rpt_seq.get(sid)
            assert last is None or f["rptSeq"] == last + 1, (sid, template, f["rptSeq"], last)
            self.rpt_seq[sid] = f["rptSeq"]

    def resting(self, security_id):
        """[(side, price, qty, soid)] in priority order, bids then offers."""
        book = self.books.get(security_id, {})
        bids = sorted((o["price"], s) for s, o in book.items() if o["side"] == "BUY")
        offers = sorted((o["price"], s) for s, o in book.items() if o["side"] == "SELL")
        bids.sort(key=lambda ps: (-ps[0], ps[1]))
        return ([("BUY", p, book[s]["qty"], s) for p, s in bids] +
                [("SELL", p, book[s]["qty"], s) for p, s in offers])
