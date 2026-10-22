"""Layer 3: market data publisher.

Turns engine outcomes into B3 messages following B3's event rules
(docs/FEED_GENERATOR_CONTEXT.md sections 5 and 6, spec/LAYOUTS.md
section 12). Owns rptSeq, matchEventIndicator, tradeID and per-instrument
session statistics.

Each call returns one or more events as a list of Published records. A
record's fields dict is the intent (what the publisher set; optional fields
left out are null) and its data is always encode(template_id, fields), so
the bytes can never disagree with the intent.

Outcome -> messages (one event each, EndOfEvent only on the last message):
* Rested: Order_MBO NEW.
* Cancelled: DeleteOrder_MBO.
* SizeReduced: Order_MBO CHANGE (same soid, mDEntryPrevSize = old qty).
* Requeued: DeleteOrder_MBO (old) then Order_MBO NEW (new soid).
* MassCancelled: MassDeleteOrders_MBO DELETE_THRU, nothing if no orders went.
* Executed: ExecutionSummary; DeleteOrder_MBO for a replaced order (crossing
  price change); one Trade per fill; per resting order hit DeleteOrder_MBO
  (fully filled) or Order_MBO CHANGE (partially); Order_MBO NEW for a resting
  remainder; OpeningPrice/HighPrice/LowPrice as needed; ExecutionStatistics.
* Unfilled, Rejected: nothing.
Session start/end and other standalone statistics or status messages each
form their own event and carry EndOfEvent.

Resets (spec/LAYOUTS.md sections 12.1, 12.2, 12.5, 12.9):
* sequence_reset: SequenceReset, alone (no matchEventIndicator, no event).
* channel_reset: ChannelReset as its own event, 0xA0 (RecoveryMsg +
  EndOfEvent); Guidelines p.47 draws it outside the recovery bracket.
* rebuild: one event per instrument, EmptyBook then an Order_MBO NEW for
  every resting order in priority order, all with RecoveryMsg (0x20), the
  last 0xA0. Orders keep their secondaryOrderID and mDInsertTimestamp;
  transactTime is the rebuild time. EmptyBook has no rptSeq; the
  instrument's rptSeq restarts at 1 on the message after it.
Statistics survive every reset; only start_session (TRADING_SESSION_CHANGE)
clears them (section 12.9; LAYOUTS.md section 13 on Message Reference 9.2.7).

Rare messages (step 7c; inferences recorded in docs/FEED_GENERATOR_CONTEXT.md):
* trade_bust: TradeBust copying an earlier Trade (securityID, session,
  price, size, tradeID, tradeDate), its own event (0x80). Statistics are
  left unchanged.
* news_parts: News split into parts (section 12.10): same newsID,
  partNumber 1..partCount, totalTextLength = the whole text's bytes,
  headline in part 1 only, each part a standalone message (0x80). No rptSeq
  (the template has none). The last part is annotated with the reassembled
  text for the intent log.
* unknown: a message with a templateId the schema does not have (schema
  evolution tests); publish(..., block_extra, version) encodes an event with
  a larger blockLength (zero bytes) and another header version.

Inferences (B3 does not specify them) are recorded as open items G, H and I
in docs/FEED_GENERATOR_CONTEXT.md section 12.

Must not: know about packet boundaries.
"""

import dataclasses
from dataclasses import dataclass

from b3gen import encode, engine, layouts

# ---------------------------------------------------------------------------
# Schema values, looked up by name so nothing is hard-coded
# ---------------------------------------------------------------------------

ENUMS = layouts.ENUMS


def _template_id(name):
    return layouts.TEMPLATES_BY_NAME[name].id


def _set_bit(set_name, choice):
    bit = next(b for b, n in layouts.SETS[set_name].items() if n == choice)
    return 1 << bit


T_ORDER = _template_id("Order_MBO_50")
T_DELETE = _template_id("DeleteOrder_MBO_51")
T_MASS_DELETE = _template_id("MassDeleteOrders_MBO_52")
T_TRADE = _template_id("Trade_53")
T_EXEC_SUMMARY = _template_id("ExecutionSummary_55")
T_EXEC_STATS = _template_id("ExecutionStatistics_56")
T_OPENING = _template_id("OpeningPrice_15")
T_HIGH = _template_id("HighPrice_24")
T_LOW = _template_id("LowPrice_25")
T_CLOSING = _template_id("ClosingPrice_17")
T_PRICE_BAND = _template_id("PriceBand_22")
T_QTY_BAND = _template_id("QuantityBand_21")
T_SETTLEMENT = _template_id("SettlementPrice_28")
T_OPEN_INTEREST = _template_id("OpenInterest_29")
T_GROUP_PHASE = _template_id("SecurityGroupPhase_10")
T_SEQUENCE_RESET = _template_id("SequenceReset_1")
T_EMPTY_BOOK = _template_id("EmptyBook_9")
T_CHANNEL_RESET = _template_id("ChannelReset_11")
T_NEWS = _template_id("News_5")
T_TRADE_BUST = _template_id("TradeBust_57")

END_OF_EVENT = _set_bit("MatchEventIndicator", "EndOfEvent")
RECOVERY_MSG = _set_bit("MatchEventIndicator", "RecoveryMsg")
REGULAR_TRADE = _set_bit("TradeCondition", "RegularTrade")
OPENING_TRADE = _set_bit("TradeCondition", "OpeningPrice")
SAME_PRICE = _set_bit("TradeCondition", "LastTradeAtTheSamePrice")

NEW = ENUMS["MDUpdateAction"]["NEW"]
CHANGE = ENUMS["MDUpdateAction"]["CHANGE"]
DELETE_THRU = ENUMS["MDUpdateAction"]["DELETE_THRU"]
ENTRY_TYPE = {engine.BUY: ENUMS["MDEntryType"]["BID"], engine.SELL: ENUMS["MDEntryType"]["OFFER"]}
AGGRESSOR = {engine.BUY: ENUMS["AggressorSide"]["BUY"], engine.SELL: ENUMS["AggressorSide"]["SELL"]}
REGULAR_SESSION = ENUMS["TradingSessionID"]["REGULAR_TRADING_SESSION"]
OPEN_PHASE = ENUMS["TradingSessionSubID"]["OPEN"]
TRADING_SESSION_CHANGE = ENUMS["SecurityTradingEvent"]["TRADING_SESSION_CHANGE"]
FLAG_DAILY = ENUMS["OpenCloseSettlFlag"]["DAILY"]
FLAG_PREVIOUS_DAY = ENUMS["OpenCloseSettlFlag"]["ENTRY_FROM_PREVIOUS_BUSINESS_DAY"]
HARD_LIMIT = ENUMS["PriceBandType"]["HARD_LIMIT"]
PRICE_UNIT = ENUMS["PriceLimitType"]["PRICE_UNIT"]
SETTLE_FINAL = ENUMS["SettlPriceType"]["FINAL"]
PRICE_TYPE_PU = ENUMS["PriceType"]["PU"]


def _field(template_id, name):
    return next(f for f in layouts.TEMPLATES[template_id].fields if f.name == name)


# Engine prices are Price mantissas (x10^-4). ClosingPrice and netChgPrevDay
# use 8 decimals; the factor comes from the schema exponents.
PRICE_EXPONENT = _field(T_TRADE, "mDEntryPx").exponent
PRICE8_FACTOR = 10 ** (PRICE_EXPONENT - _field(T_CLOSING, "mDEntryPx").exponent)
assert PRICE8_FACTOR == 10 ** (PRICE_EXPONENT - _field(T_EXEC_STATS, "netChgPrevDay").exponent)

HAS_RPTSEQ = {tid: any(f.name == "rptSeq" for f in t.fields)
              for tid, t in layouts.TEMPLATES.items()}
HAS_MATCH_EVENT = {tid: any(f.name == "matchEventIndicator" for f in t.fields)
                   for tid, t in layouts.TEMPLATES.items()}


def round_half_even(numerator, denominator):
    """numerator / denominator rounded to an integer, ties to even."""
    q, r = divmod(numerator, denominator)
    twice = 2 * r
    if twice > denominator or (twice == denominator and q % 2):
        q += 1
    return q


# ---------------------------------------------------------------------------
# Inputs and outputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Instrument:
    name: str
    security_id: int
    channel: int
    segment: str
    security_group: str
    tick: int
    is_future: bool


@dataclass(frozen=True)
class SessionValues:
    prev_close: int              # Price mantissa (x10^-4)
    low_limit: int
    high_limit: int
    avg_daily_traded_qty: int
    max_trade_vol: int
    settlement_price: int | None = None   # futures only
    settl_date: int | None = None
    open_interest: int | None = None


@dataclass(frozen=True)
class Published:
    time_ns: int
    channel: int
    instrument: str | None       # None for SecurityGroupPhase
    template_id: int
    fields: dict
    data: bytes
    block_length: int | None = None   # header blockLength if not the schema's
    version: int | None = None        # header version if not the schema's
    annotations: dict | None = None   # extra intent log keys (News text)


class PublishError(ValueError):
    """Outcome or session input the publisher cannot handle."""


class _State:
    """Per-instrument publisher state."""

    def __init__(self):
        self.rpt_seq = 0
        self.reset_session(trade_date=None, prev_close=None)

    def reset_session(self, trade_date, prev_close):
        self.trade_date = trade_date
        self.prev_close = prev_close
        self.next_trade_id = 1
        self.volume = 0
        self.notional = 0        # sum of price * qty
        self.trades = 0
        self.high = None
        self.low = None
        self.last_price = None
        self.opened = False
        self.close = None        # set by end_session


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------

class Publisher:
    def __init__(self, instruments):
        self.instruments = {i.name: i for i in instruments}
        self._state = {name: _State() for name in self.instruments}
        self._in_session = False

    # -- sessions -----------------------------------------------------------

    def start_session(self, time_ns, trade_date, values):
        """Reset statistics and send the session start sequence.

        SecurityGroupPhase (TRADING_SESSION_CHANGE) goes first: clients clear
        statistics on it, so the new session's ClosingPrice and bands must
        follow it."""
        missing = set(self.instruments) - set(values)
        if missing:
            raise PublishError(f"no session values for {sorted(missing)}")
        events = []
        groups = []
        for inst in self.instruments.values():
            key = (inst.security_group, inst.channel)
            if key not in groups:
                groups.append(key)
        for group, channel in groups:
            events.append(self._event(time_ns, [(channel, None, T_GROUP_PHASE, {
                "securityGroup": group, "tradingSessionID": REGULAR_SESSION,
                "tradingSessionSubID": OPEN_PHASE,
                "securityTradingEvent": TRADING_SESSION_CHANGE,
                "tradeDate": trade_date, "transactTime": time_ns})]))

        for inst in self.instruments.values():
            v = values[inst.name]
            self._state[inst.name].reset_session(trade_date, v.prev_close)
            sid = inst.security_id
            standalone = [
                (T_CLOSING, {"securityID": sid, "openCloseSettlFlag": FLAG_PREVIOUS_DAY,
                             "mDEntryPx": v.prev_close * PRICE8_FACTOR,
                             "tradeDate": trade_date, "mDEntryTimestamp": time_ns}),
                (T_PRICE_BAND, {"securityID": sid, "priceBandType": HARD_LIMIT,
                                "priceLimitType": PRICE_UNIT, "lowLimitPrice": v.low_limit,
                                "highLimitPrice": v.high_limit, "mDEntryTimestamp": time_ns}),
                (T_QTY_BAND, {"securityID": sid, "avgDailyTradedQty": v.avg_daily_traded_qty,
                              "maxTradeVol": v.max_trade_vol, "mDEntryTimestamp": time_ns}),
            ]
            if inst.is_future:
                if None in (v.settlement_price, v.settl_date, v.open_interest):
                    raise PublishError(f"{inst.name}: futures need settlement and open interest")
                standalone += [
                    (T_SETTLEMENT, {"securityID": sid, "tradeDate": trade_date,
                                    "mDEntryPx": v.settlement_price,
                                    "mDEntryTimestamp": time_ns, "priceType": PRICE_TYPE_PU,
                                    "settlPriceType": SETTLE_FINAL, "settlDate": v.settl_date}),
                    (T_OPEN_INTEREST, {"securityID": sid, "tradeDate": trade_date,
                                       "mDEntrySize": v.open_interest,
                                       "mDEntryTimestamp": time_ns}),
                ]
            for template_id, fields in standalone:
                events.append(self._event(time_ns, [(inst.channel, inst.name, template_id, fields)]))
        self._in_session = True
        return [m for event in events for m in event]

    def end_session(self, time_ns):
        """ClosingPrice per instrument: the session's last trade price, or
        the previous close if it did not trade."""
        self._require_session()
        out = []
        for inst in self.instruments.values():
            s = self._state[inst.name]
            close = s.last_price if s.last_price is not None else s.prev_close
            s.close = close
            out += self._event(time_ns, [(inst.channel, inst.name, T_CLOSING, {
                "securityID": inst.security_id, "openCloseSettlFlag": FLAG_DAILY,
                "mDEntryPx": close * PRICE8_FACTOR, "tradeDate": s.trade_date,
                "mDEntryTimestamp": time_ns})])
        self._in_session = False
        return out

    def closing_price(self, name):
        """The close end_session published for an instrument."""
        return self._state[name].close

    def trade_date(self, name):
        return self._state[name].trade_date

    # -- rare messages --------------------------------------------------------

    def trade_bust(self, time_ns, trade):
        """TradeBust for an earlier Published Trade."""
        if trade.template_id != T_TRADE:
            raise PublishError("trade_bust needs a Trade")
        f = trade.fields
        return self._event(time_ns, [(trade.channel, trade.instrument, T_TRADE_BUST, {
            "securityID": f["securityID"], "tradingSessionID": f["tradingSessionID"],
            "mDEntryPx": f["mDEntryPx"], "mDEntrySize": f["mDEntrySize"],
            "tradeID": f["tradeID"], "tradeDate": f["tradeDate"], "transactTime": time_ns})])

    def news_parts(self, time_ns, channel, name, news_id, headline, text, part_size,
                   source="OTHER", language="EN", block_extra=0, version=None):
        """The parts of one News as [[Published]] (one event each), to be
        sent in order. name None: market-wide (securityID null)."""
        data = text.encode("ascii")  # ASCII: byte splits never cut a character
        chunks = [data[i:i + part_size] for i in range(0, len(data), part_size)] or [b""]
        parts = []
        for number, chunk in enumerate(chunks, start=1):
            fields = {"newsSource": ENUMS["NewsSource"][source], "languageCode": language,
                      "partCount": len(chunks), "partNumber": number, "newsID": news_id,
                      "origTime": time_ns, "totalTextLength": len(data), "text": chunk}
            if name is not None:
                fields["securityID"] = self.instruments[name].security_id
            if number == 1:
                fields["headline"] = headline.encode("ascii")
            event = self._event(time_ns, [(channel, name, T_NEWS, fields)],
                                block_extra=block_extra, version=version)
            parts.append(event)
        parts[-1][0] = dataclasses.replace(parts[-1][0], annotations={"news": {
            "newsID": news_id, "headline": headline, "text": text}})
        return parts

    @staticmethod
    def unknown(time_ns, channel, template_id, body, version):
        """A message the schema does not define: header + body only."""
        if template_id in layouts.TEMPLATES:
            raise PublishError(f"template {template_id} is known")
        header = bytearray(encode.HEADER_SIZE)
        encode.write_members(header, 0, layouts.FRAMING_HEADER,
                             {"messageLength": encode.HEADER_SIZE + len(body),
                              "encodingType": encode.ENCODING_TYPE}, "unknown", use_nulls=False)
        encode.write_members(header, encode.FRAMING_HEADER_SIZE, layouts.MESSAGE_HEADER,
                             {"blockLength": len(body), "templateId": template_id,
                              "schemaId": layouts.SCHEMA_ID, "version": version},
                             "unknown", use_nulls=False)
        return Published(time_ns, channel, None, template_id, {"body": bytes(body)},
                         bytes(header) + bytes(body), block_length=len(body), version=version)

    # -- resets -------------------------------------------------------------

    def sequence_reset(self, time_ns, channel):
        """SequenceReset (no fields: newSeqNo is the constant 1)."""
        return Published(time_ns, channel, None, T_SEQUENCE_RESET, {},
                         encode.encode(T_SEQUENCE_RESET, {}))

    def channel_reset(self, time_ns, channel):
        return self._event(time_ns, [(channel, None, T_CHANNEL_RESET,
                                      {"mDEntryTimestamp": time_ns})], recovery=True)

    def rebuild(self, time_ns, name, orders):
        """EmptyBook then the instrument's resting orders (engine OrderViews,
        priority order), as one recovery event."""
        inst = self.instruments[name]
        sid = inst.security_id
        msgs = [(T_EMPTY_BOOK, {"securityID": sid, "mDEntryTimestamp": time_ns})]
        msgs += [self._order_new(sid, time_ns, o) for o in orders]
        return self._event(time_ns, [(inst.channel, name, tid, f) for tid, f in msgs],
                           recovery=True)

    # -- outcomes -----------------------------------------------------------

    def publish(self, outcome, block_extra=0, version=None):
        """Messages for one engine outcome (one event, or nothing).
        block_extra / version: encode it as a newer schema would (zero
        bytes appended to each root block, another header version)."""
        self._require_session()
        kind = type(outcome)
        if kind in (engine.Unfilled, engine.Rejected):
            return []
        inst = self.instruments.get(outcome.instrument)
        if inst is None:
            raise PublishError(f"unknown instrument {outcome.instrument}")
        sid = inst.security_id
        t = outcome.time_ns

        if kind is engine.Rested:
            msgs = [self._order_new(sid, t, outcome)]
        elif kind is engine.Cancelled:
            msgs = [self._delete(sid, t, outcome.order.side, outcome.order.price,
                                 outcome.order.qty, outcome.order.soid)]
        elif kind is engine.SizeReduced:
            msgs = [self._order_change(sid, t, outcome.side, outcome.price, outcome.new_qty,
                                       outcome.old_qty, outcome.soid, outcome.firm,
                                       outcome.insert_time_ns)]
        elif kind is engine.Requeued:
            msgs = [self._delete(sid, t, outcome.side, outcome.old_price, outcome.old_qty,
                                 outcome.old_soid),
                    (T_ORDER, {"securityID": sid, "mDUpdateAction": NEW,
                               "mDEntryType": ENTRY_TYPE[outcome.side],
                               "mDEntryPx": outcome.new_price, "mDEntrySize": outcome.new_qty,
                               "enteringFirm": outcome.firm,
                               "mDInsertTimestamp": outcome.new_insert_time_ns,
                               "secondaryOrderID": outcome.new_soid, "transactTime": t})]
        elif kind is engine.MassCancelled:
            if not outcome.orders:
                return []  # no effect on the book, nothing to publish (choice 1)
            msgs = [(T_MASS_DELETE, {"securityID": sid, "mDUpdateAction": DELETE_THRU,
                                     "mDEntryType": ENTRY_TYPE[outcome.side],
                                     "transactTime": t})]
        elif kind is engine.Executed:
            msgs = self._execution(inst, outcome)
        else:
            raise PublishError(f"unknown outcome {outcome!r}")
        return self._event(t, [(inst.channel, inst.name, tid, f) for tid, f in msgs],
                           block_extra=block_extra, version=version)

    def _execution(self, inst, ex):
        sid = inst.security_id
        s = self._state[inst.name]
        t = ex.time_ns
        msgs = [(T_EXEC_SUMMARY, {
            "securityID": sid, "aggressorSide": AGGRESSOR[ex.side],
            "lastPx": ex.fills[-1].price, "fillQty": ex.filled_qty,
            "aggressorTime": t, "transactTime": t})]
        if ex.replaced is not None:
            # Crossing price change: the old order leaves the book first
            # (inference, open item G).
            r = ex.replaced
            msgs.append(self._delete(sid, t, r.side, r.price, r.qty, r.soid))

        first_trade_of_session = not s.opened
        high_before, low_before = s.high, s.low
        for fill in ex.fills:
            condition = REGULAR_TRADE
            if not s.opened:
                condition |= OPENING_TRADE
                s.opened = True
            if s.last_price == fill.price:
                condition |= SAME_PRICE
            buyer, seller = ((ex.firm, fill.resting_firm) if ex.side == engine.BUY
                             else (fill.resting_firm, ex.firm))
            msgs.append((T_TRADE, {
                "securityID": sid, "tradingSessionID": REGULAR_SESSION,
                "tradeCondition": condition, "mDEntryPx": fill.price, "mDEntrySize": fill.qty,
                "tradeID": s.next_trade_id, "mDEntryBuyer": buyer, "mDEntrySeller": seller,
                "tradeDate": s.trade_date, "transactTime": t}))
            s.next_trade_id += 1
            s.volume += fill.qty
            s.notional += fill.price * fill.qty
            s.trades += 1
            s.high = fill.price if s.high is None else max(s.high, fill.price)
            s.low = fill.price if s.low is None else min(s.low, fill.price)
            s.last_price = fill.price

        resting_side = engine.SELL if ex.side == engine.BUY else engine.BUY
        for fill in ex.fills:
            if fill.resting_qty_after == 0:
                msgs.append(self._delete(sid, t, resting_side, fill.price,
                                         fill.resting_qty_before, fill.resting_soid))
            else:
                msgs.append(self._order_change(sid, t, resting_side, fill.price,
                                               fill.resting_qty_after, fill.resting_qty_before,
                                               fill.resting_soid, fill.resting_firm,
                                               fill.resting_insert_time_ns))
        if ex.remainder is not None:
            msgs.append(self._order_new(sid, t, ex.remainder))

        # Statistics (inference, open item H): OpeningPrice, HighPrice and
        # LowPrice on the session's first trade; later, High/Low only when the
        # extreme changed, once per event with the final value.
        if first_trade_of_session:
            opening = ex.fills[0].price
            fields = {"securityID": sid, "mDUpdateAction": NEW,
                      "openCloseSettlFlag": FLAG_DAILY, "mDEntryPx": opening,
                      "tradeDate": s.trade_date, "mDEntryTimestamp": t}
            if s.prev_close is not None:
                fields["netChgPrevDay"] = (opening - s.prev_close) * PRICE8_FACTOR
            msgs.append((T_OPENING, fields))
        if first_trade_of_session or s.high != high_before:
            msgs.append((T_HIGH, {"securityID": sid, "mDUpdateAction": NEW,
                                  "tradeDate": s.trade_date, "mDEntryPx": s.high,
                                  "mDEntryTimestamp": t}))
        if first_trade_of_session or s.low != low_before:
            msgs.append((T_LOW, {"securityID": sid, "mDUpdateAction": NEW,
                                 "tradeDate": s.trade_date, "mDEntryPx": s.low,
                                 "mDEntryTimestamp": t}))
        stats = {"securityID": sid, "tradingSessionID": REGULAR_SESSION,
                 "tradeDate": s.trade_date, "tradeVolume": s.volume,
                 "vwapPx": round_half_even(s.notional, s.volume),
                 "numberOfTrades": s.trades, "mDEntryTimestamp": t}
        if s.prev_close is not None:
            stats["netChgPrevDay"] = (s.last_price - s.prev_close) * PRICE8_FACTOR
        msgs.append((T_EXEC_STATS, stats))
        return msgs

    # -- message builders -----------------------------------------------------

    @staticmethod
    def _order_new(sid, t, rested):
        return (T_ORDER, {"securityID": sid, "mDUpdateAction": NEW,
                          "mDEntryType": ENTRY_TYPE[rested.side], "mDEntryPx": rested.price,
                          "mDEntrySize": rested.qty, "enteringFirm": rested.firm,
                          "mDInsertTimestamp": rested.insert_time_ns,
                          "secondaryOrderID": rested.soid, "transactTime": t})

    @staticmethod
    def _order_change(sid, t, side, price, qty, prev_qty, soid, firm, insert_time_ns):
        return (T_ORDER, {"securityID": sid, "mDUpdateAction": CHANGE,
                          "mDEntryType": ENTRY_TYPE[side], "mDEntryPx": price,
                          "mDEntrySize": qty, "enteringFirm": firm,
                          "mDInsertTimestamp": insert_time_ns, "secondaryOrderID": soid,
                          "transactTime": t, "mDEntryPrevSize": prev_qty})

    @staticmethod
    def _delete(sid, t, side, price, qty, soid):
        # mDEntrySize: remaining qty just before the deletion.
        return (T_DELETE, {"securityID": sid, "mDEntryType": ENTRY_TYPE[side],
                           "mDEntrySize": qty, "secondaryOrderID": soid,
                           "transactTime": t, "mDEntryPx": price})

    # -- events ---------------------------------------------------------------

    def _require_session(self):
        if not self._in_session:
            raise PublishError("no trading session in progress; call start_session first")

    def _event(self, time_ns, messages, recovery=False, block_extra=0, version=None):
        """Finish one event: matchEventIndicator (EndOfEvent on the last
        message only, RecoveryMsg on every message of a recovery), rptSeq
        per instrument in message order, then encode."""
        out = []
        last = len(messages) - 1
        flags = RECOVERY_MSG if recovery else 0
        for i, (channel, instrument, template_id, fields) in enumerate(messages):
            fields = dict(fields)
            if HAS_MATCH_EVENT[template_id]:
                fields["matchEventIndicator"] = flags | (END_OF_EVENT if i == last else 0)
            elif i == last:
                raise PublishError(f"template {template_id} cannot end an event")
            if template_id == T_EMPTY_BOOK:
                self._state[instrument].rpt_seq = 0  # restarts at 1 with the next message
            elif HAS_RPTSEQ[template_id]:
                state = self._state[instrument]
                state.rpt_seq += 1
                fields["rptSeq"] = state.rpt_seq
            block_length = layouts.TEMPLATES[template_id].block_length + block_extra \
                if block_extra else None
            out.append(Published(time_ns, channel, instrument, template_id, fields,
                                 encode.encode(template_id, fields, block_length=block_length,
                                               version=version),
                                 block_length=block_length, version=version))
        return out

