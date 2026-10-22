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

Inferences (B3 does not specify them) are recorded as open items G, H and I
in docs/FEED_GENERATOR_CONTEXT.md section 12.

Must not: know about packet boundaries.
"""

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

END_OF_EVENT = _set_bit("MatchEventIndicator", "EndOfEvent")
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
            out += self._event(time_ns, [(inst.channel, inst.name, T_CLOSING, {
                "securityID": inst.security_id, "openCloseSettlFlag": FLAG_DAILY,
                "mDEntryPx": close * PRICE8_FACTOR, "tradeDate": s.trade_date,
                "mDEntryTimestamp": time_ns})])
        self._in_session = False
        return out

    # -- outcomes -----------------------------------------------------------

    def publish(self, outcome):
        """Messages for one engine outcome (one event, or nothing)."""
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
        return self._event(t, [(inst.channel, inst.name, tid, f) for tid, f in msgs])

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

    def _event(self, time_ns, messages):
        """Finish one event: matchEventIndicator (EndOfEvent on the last
        message only), rptSeq per instrument in message order, then encode."""
        out = []
        last = len(messages) - 1
        for i, (channel, instrument, template_id, fields) in enumerate(messages):
            fields = dict(fields)
            if HAS_MATCH_EVENT[template_id]:
                fields["matchEventIndicator"] = END_OF_EVENT if i == last else 0
            elif i == last:
                raise PublishError(f"template {template_id} cannot end an event")
            if HAS_RPTSEQ[template_id]:
                state = self._state[instrument]
                state.rpt_seq += 1
                fields["rptSeq"] = state.rpt_seq
            out.append(Published(time_ns, channel, instrument, template_id, fields,
                                 encode.encode(template_id, fields)))
        return out

