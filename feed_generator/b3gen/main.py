"""Support: wiring the layers together (the CLI arrives in step 8).

run(config) drives one trading session for Feed A:

    flow -> engine -> publisher -> packetizer -> pcap
                          |             |
                          +--> golden <-+   (intent log, book key)

start_session at the beginning, end_session at the end. Every published
message is tagged with its index so the packetizer can report where it
landed, which is what the intent log and book key are keyed by.

RunConfig and default_config() hold the parameters for now; config.py and a
config file take over in step 8. Instrument mapping, securityIDs, prices and
multicast groups in the default are invented (channel numbers are real B3
Binary UMDF channels; groups are from 233.252.0.0/14, MCAST-TEST-NET).

Must not: contain generation logic; it only wires the layers together.
See docs/FEED_GENERATOR_CONTEXT.md sections 4 and 11.
"""

import datetime
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from b3gen import golden, pcap
from b3gen.engine import Engine
from b3gen.flow import FlowInstrument, FlowParams, OrderFlow
from b3gen.packet import Packetizer
from b3gen.publisher import Instrument, Publisher, SessionValues


@dataclass(frozen=True)
class InstrumentConfig:
    name: str
    security_id: int
    channel: int
    segment: str
    security_group: str
    tick: int                 # Price mantissa units (x10^-4)
    lot: int
    is_future: bool
    prev_close: int
    low_limit: int
    high_limit: int
    avg_daily_traded_qty: int
    max_trade_vol: int
    settlement_price: int | None = None
    open_interest: int | None = None


@dataclass(frozen=True)
class RunConfig:
    seed: int
    out_dir: Path
    target_messages: int          # stop once this many messages are published
    instruments: tuple
    flow: FlowParams
    start_ns: int
    trade_date: int               # days since epoch
    channel_map: dict             # channel -> (multicast group, port)
    src_ip: str = "10.0.0.1"
    src_port: int = 50000
    window_ns: int | None = 5000
    book_key_every: int = 1000    # events between book key checkpoints
    record: bool = False          # keep per-arrival records (tests)


@dataclass
class RunResult:
    pcap_path: Path
    intent_path: Path
    book_key_path: Path
    messages: int
    events: int
    packets: int
    heartbeats: int
    outcomes: Counter
    instructions: Counter
    max_empty_side_streak: dict   # instrument -> most consecutive arrivals seeing an empty side
    final_resting: dict           # instrument -> resting orders at the end of the run
    arrivals: list = field(default_factory=list)  # (Arrival, instruction) when record=True
    regimes: list = field(default_factory=list)   # (start, end, regime) when record=True
    message_times: list = field(default_factory=list)  # (channel, time_ns) per published message, when record=True


def _band(price, pct):
    return price * (100 - pct) // 100, price * (100 + pct) // 100


def default_config(seed=1, target_messages=20_000, out_dir=Path("data"), **overrides):
    start = datetime.datetime(2026, 10, 19, 13, 0, tzinfo=datetime.timezone.utc)  # 10:00 BRT
    trade_date = (start.date() - datetime.date(1970, 1, 1)).days

    def inst(name, sid, channel, segment, group, tick, lot, future, close, adtv, maxvol,
             settle=None, oi=None):
        low, high = _band(close, 8)
        return InstrumentConfig(name, sid, channel, segment, group, tick, lot, future, close,
                                low, high, adtv, maxvol, settle, oi)

    instruments = (
        # WIN: tick 5 points; DOL: tick 0.5; equities: tick 0.01. Prices x10^-4.
        inst("WIN", 200_000_001, 68, "DRV", "IND", 50_000, 1, True, 1_300_000_000, 0, 5_000,
             settle=1_300_000_000, oi=450_000),
        inst("DOL", 200_000_002, 68, "DRV", "DOL", 5_000, 5, True, 54_000_000, 0, 5_000,
             settle=54_000_000, oi=900_000),
        inst("PETR4", 100_000_001, 70, "EQT", "EQ1", 100, 100, False, 385_000,
             40_000_000, 10_000_000),
        inst("VALE3", 100_000_002, 70, "EQT", "EQ1", 100, 100, False, 612_000,
             20_000_000, 5_000_000),
    )
    # Spikes run ~1.6M arrivals/s per channel: filling 1400 B inside the 5 us
    # window takes ~15 messages, so slower spikes (~200k/s) almost never do.
    flow = FlowParams(rates={"WIN": (400, 4000, 960_000), "DOL": (150, 1500, 640_000),
                             "PETR4": (250, 2500, 960_000), "VALE3": (150, 1500, 640_000)},
                      calm_mean_ns=1_000_000_000)
    config = dict(
        seed=seed, out_dir=Path(out_dir), target_messages=target_messages,
        instruments=instruments, flow=flow, start_ns=int(start.timestamp()) * 1_000_000_000,
        trade_date=trade_date,
        channel_map={68: ("233.252.8.68", 30068), 70: ("233.252.8.70", 30070)},
    )
    config.update(overrides)
    return RunConfig(**config)


def run(config):
    insts = config.instruments
    engine = Engine([(i.name, i.tick, i.lot, i.segment) for i in insts])
    publisher = Publisher([Instrument(i.name, i.security_id, i.channel, i.segment,
                                      i.security_group, i.tick, i.is_future) for i in insts])
    flow = OrderFlow([FlowInstrument(i.name, i.tick, i.lot, i.low_limit, i.high_limit,
                                     i.prev_close) for i in insts],
                     config.flow, config.seed, config.start_ns)
    channels = sorted({i.channel for i in insts})
    packetizer = Packetizer(channels, config.start_ns, window_ns=config.window_ns)

    published = []
    checkpoints = []
    last_tag_on_channel = {}
    state = {"events": 0}

    def emit(messages):
        """Packetize one event and take a book key checkpoint every N events."""
        for m in messages:
            tag = len(published)
            published.append(m)
            packetizer.add(m.time_ns, m.channel, m.data, tag)
            last_tag_on_channel[m.channel] = tag
        state["events"] += 1
        if state["events"] % config.book_key_every == 0:
            books = {i.name: (last_tag_on_channel[i.channel], engine.books[i.name].top(5))
                     for i in insts if i.channel in last_tag_on_channel}
            checkpoints.append((len(published) - 1, books))

    values = {i.name: SessionValues(
        prev_close=i.prev_close, low_limit=i.low_limit, high_limit=i.high_limit,
        avg_daily_traded_qty=i.avg_daily_traded_qty, max_trade_vol=i.max_trade_vol,
        settlement_price=i.settlement_price,
        settl_date=config.trade_date - 1 if i.is_future else None,
        open_interest=i.open_interest) for i in insts}
    for m in publisher.start_session(config.start_ns, config.trade_date, values):
        emit([m])  # standalone: each its own event

    outcomes = Counter()
    instructions = Counter()
    streak = {i.name: 0 for i in insts}
    max_streak = {i.name: 0 for i in insts}
    arrivals = []
    t = config.start_ns
    while len(published) < config.target_messages:
        arrival = flow.next_arrival()
        t = arrival.time_ns
        name = arrival.instrument
        top = engine.books[name].top(5)
        streak[name] = streak[name] + 1 if not (top[0] and top[1]) else 0
        max_streak[name] = max(max_streak[name], streak[name])
        instruction = flow.decide(arrival, top)
        instructions[type(instruction).__name__] += 1
        if config.record:
            arrivals.append((arrival, instruction))
        outcome = engine.submit(name, instruction)
        outcomes[type(outcome).__name__] += 1
        flow.on_outcome(outcome)
        messages = publisher.publish(outcome)
        if messages:
            emit(messages)
    for m in publisher.end_session(t):
        emit([m])

    packetized = packetizer.finish(t)
    out = Path(config.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = RunResult(
        pcap_path=out / "feedA.pcap", intent_path=out / "intent.jsonl",
        book_key_path=out / "book_key.jsonl", messages=len(published),
        events=state["events"], packets=len(packetized.packets),
        heartbeats=len(packetized.heartbeats), outcomes=outcomes, instructions=instructions,
        max_empty_side_streak=max_streak,
        final_resting={i.name: len(engine.books[i.name].resting_orders()) for i in insts},
        arrivals=arrivals,
        regimes=list(flow.clock.segments) if config.record else [],
        message_times=[(m.channel, m.time_ns) for m in published] if config.record else [])
    pcap.write_pcap(result.pcap_path, packetized.packets, channel_map=config.channel_map,
                    src_ip=config.src_ip, src_port=config.src_port)
    golden.write_jsonl(result.intent_path, golden.intent_rows(published, packetized))
    golden.write_jsonl(result.book_key_path, golden.book_key_rows(checkpoints, packetized))
    return result
