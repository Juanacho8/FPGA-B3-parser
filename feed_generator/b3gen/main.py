"""Support: wiring the layers together (the CLI arrives in step 8).

run(config) drives one trading session for Feed A, and run_scenario(config,
name) writes one scenario (clean, loss, gap, gap_one, idle) with both feeds and a
manifest into out_dir/name:

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

import dataclasses
import datetime
import heapq
import itertools
import json
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from b3gen import golden, pcap, scenarios
from b3gen import engine as eng
from b3gen.engine import Engine
from b3gen.flow import FlowInstrument, FlowParams, OrderFlow
from b3gen.packet import Packetizer
from b3gen.publisher import T_TRADE, Instrument, Publisher, SessionValues


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
    band_bps: int = 800           # PriceBand half-width around the previous close
    book_key_every: int = 1000    # events between book key checkpoints
    record: bool = False          # keep per-arrival records (tests)
    channel_map_b: dict = None    # Feed B: channel -> (multicast group, port)
    feed_b: scenarios.FeedBParams = scenarios.FeedBParams()
    loss: scenarios.BurstParams = scenarios.BurstParams(0.01, 3)
    gap: scenarios.BurstParams = scenarios.BurstParams(0.001, 2)
    idle: scenarios.IdleParams | None = None  # pause the flow once (idle scenario)
    resets: tuple = ()            # scenarios.Reset
    injections: tuple = ()        # scenarios.Inject


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
    # Flow time: regimes do not move with an idle pause.
    message_times: list = field(default_factory=list)  # (channel, time_ns) per published message, when record=True
    resets: list = field(default_factory=list)    # one dict per reset applied (tags, not placements)
    injections: list = field(default_factory=list)  # one dict per injection (tags, not placements)


def _band(price, bps):
    """PriceBand: +-bps basis points around the previous close."""
    return price * (10_000 - bps) // 10_000, price * (10_000 + bps) // 10_000


def default_config(seed=1, target_messages=20_000, out_dir=Path("data"), **overrides):
    band_bps = overrides.get("band_bps", RunConfig.band_bps)
    start = datetime.datetime(2026, 10, 19, 13, 0, tzinfo=datetime.timezone.utc)  # 10:00 BRT
    trade_date = (start.date() - datetime.date(1970, 1, 1)).days

    def inst(name, sid, channel, segment, group, tick, lot, future, close, adtv, maxvol,
             settle=None, oi=None):
        low, high = _band(close, band_bps)
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
        channel_map_b={68: ("233.252.9.68", 31068), 70: ("233.252.9.70", 31070)},
    )
    config.update(overrides)
    return RunConfig(**config)


@dataclass
class ScenarioResult:
    run: RunResult                # pcap_path is Feed A
    feed_b_path: Path
    manifest_path: Path
    manifest: dict


SCENARIOS = ("clean", "loss", "gap", "gap_one", "idle") + scenarios.RESET_KINDS + \
    ("edge", "rare", "schema", "malformed")
MALFORMED_MESSAGES = 1500         # the malformed file is small
EXTENDED_BLOCK = 8                # schema evolution: bytes added to the root block
EXTENDED_VERSION = 18
UNKNOWN_TEMPLATE = 999
DAY_NS = 86_400 * 1_000_000_000
START_OF_DAY_NS = 2_500_000_000   # stream up this long before the session: 3 heartbeats


def run(config):
    """Feed A, intent log and book key in out_dir."""
    result, published, packetized, checkpoints = _generate(config)
    _write(config, result, published, packetized, checkpoints, packetized.packets)
    return result


def run_scenario(config, name):
    """Both feeds, intent log, book key and manifest in out_dir/name."""
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario {name!r}; one of {SCENARIOS}")
    if name == "idle" and config.idle is None:
        config = dataclasses.replace(
            config, idle=scenarios.IdleParams(after_messages=config.target_messages // 2))
    elif name != "idle":
        config = dataclasses.replace(config, idle=None)
    if name in scenarios.RESET_KINDS:
        target = {"channel_reset": config.instruments[0].channel,
                  "instrument_reset": config.instruments[0].name}.get(name)
        config = dataclasses.replace(
            config, resets=(scenarios.Reset(name, config.target_messages // 2, target),))
        if name == "weekly_start":  # the file opens on the Friday before
            config = dataclasses.replace(config, start_ns=config.start_ns - 3 * DAY_NS,
                                         trade_date=config.trade_date - 3)
    else:
        config = dataclasses.replace(config, resets=())
    config = dataclasses.replace(config, injections=_injections(name, config))
    if name == "malformed":
        config = dataclasses.replace(config, target_messages=MALFORMED_MESSAGES)
    config = dataclasses.replace(config, out_dir=Path(config.out_dir) / name)
    loss = config.loss if name == "loss" else None
    gap = config.gap if name == "gap" else None

    result, published, packetized, checkpoints = _generate(config)
    on_channel = {}
    for i in config.instruments:
        on_channel.setdefault(i.channel, []).append(i.name)
    faults, lost = None, []
    if name == "malformed":
        packetized, faults, lost = scenarios.malform(packetized, config.seed)
    chosen = [scenarios.single_instrument_packet(packetized, published)] \
        if name == "gap_one" else []
    feeds = scenarios.build_feeds(packetized, config.seed, feed_b=config.feed_b,
                                  loss=loss, gap=gap, gap_positions=chosen)
    _write(config, result, published, packetized, checkpoints, feeds.a)
    out = Path(config.out_dir)
    feed_b_path = out / "feedB.pcap"
    pcap.write_pcap(feed_b_path, feeds.b, channel_map=config.channel_map_b,
                    src_ip=config.src_ip, src_port=config.src_port)
    unreliable = scenarios.unreliable_books(feeds.gap_positions, packetized, published,
                                            on_channel)
    if lost:
        unreliable = scenarios.unreliable_from_lost(lost, packetized, published, on_channel)
    params = {"feed_b": dataclasses.asdict(config.feed_b),
              "loss": dataclasses.asdict(loss) if loss else None,
              "gap": dataclasses.asdict(gap) if gap else None,
              "idle": dataclasses.asdict(config.idle) if config.idle else None,
              "gap_packets": [list(k) for k in feeds.missing_b] if chosen else None}
    manifest = {"scenario": name, "seed": config.seed, "params": params,
                "missing": {"A": [list(k) for k in feeds.missing_a],
                            "B": [list(k) for k in feeds.missing_b]},
                "unreliable_books": {n: list(k) for n, k in sorted(unreliable.items())},
                "resets": [_reset_record(r, packetized) for r in result.resets],
                "injections": [_injection_record(r, packetized) for r in result.injections]}
    if faults is not None:
        manifest["faults"] = faults
        manifest["expected_client_behavior"] = (
            "Process the complete messages before the fault, discard the rest of the "
            "packet, carry on with sequencing. A datagram shorter than the 16-byte packet "
            "header is discarded and uses up no sequence number.")
    if name == "schema":
        manifest["expected_client_behavior"] = (
            "Decode the known fields using the header's blockLength and version, skip the "
            "extra root-block bytes, skip unknown templates by messageLength. The book "
            "still matches the book key.")
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return ScenarioResult(result, feed_b_path, manifest_path, manifest)


def _injections(name, config):
    """The one-off actions of the 7c scenarios, at half the target."""
    half = config.target_messages // 2
    by_name = {i.name: i for i in config.instruments}
    inject = scenarios.Inject
    if name == "edge":
        return (inject("mass_cancel", half, (("instrument", "PETR4"), ("side", eng.BUY))),
                inject("mass_cancel", half, (("instrument", "PETR4"), ("side", eng.SELL))),
                inject("sweep", half, (("instrument", "WIN"), ("side", eng.BUY),
                                       ("orders", 30))))
    if name == "rare":
        return (inject("trade_bust", half, (("instrument", "WIN"),)),
                inject("news", half, (("channel", by_name["WIN"].channel),
                                      ("instrument", "WIN"), ("text_bytes", 300),
                                      ("part_size", 1000), ("spread", 0))),
                inject("news", half + 10, (("channel", 70), ("instrument", None),
                                           ("text_bytes", 3000), ("part_size", 1000),
                                           ("spread", 40))))
    if name == "schema":
        return (inject("extend", half, (("count", 3),)),
                inject("news", half, (("channel", by_name["WIN"].channel),
                                      ("instrument", "WIN"), ("text_bytes", 200),
                                      ("part_size", 1000), ("spread", 0),
                                      ("block_extra", EXTENDED_BLOCK),
                                      ("version", EXTENDED_VERSION))),
                inject("unknown", half, (("template_id", UNKNOWN_TEMPLATE),
                                         ("body_bytes", 20))))
    return ()


def _news_text(rng, size):
    words = ["mercado", "ajuste", "vencimento", "leilao", "negociacao", "contrato",
             "indice", "taxa", "liquidacao", "circuit", "breaker", "B3", "aviso"]
    text = ""
    while len(text) < size:
        text += rng.choice(words) + " "
    return text[:size]


def _injection_record(inj, packetized):
    def at(tag):
        return golden.placement_key(packetized.placements[tag])
    out = {k: v for k, v in inj.items() if not k.endswith("tags")}
    for key in [k for k in inj if k.endswith("tags")]:
        out[key[:-4] + "placements"] = [at(tag) for tag in inj[key]]
    return out


def _reset_record(reset, packetized):
    """A reset's log entry with tags turned into wire placements."""
    def at(tag):
        return golden.placement_key(packetized.placements[tag])
    out = {k: v for k, v in reset.items() if not k.endswith("tags")}
    out["first"] = at(reset["tags"][0]) if reset["tags"] else None
    out["last"] = at(reset["tags"][-1]) if reset["tags"] else None
    out["sequence_resets"] = {str(ch): at(tag) for ch, tag in reset["sequence_reset_tags"].items()}
    out["rebuilds"] = {name: [at(first), at(last)]
                       for name, (first, last) in reset["rebuild_tags"].items()}
    return out


def _generate(config):
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
    cleared = set()  # instruments a ChannelReset emptied, not rebuilt yet
    last_tag_on_channel = {}
    state = {"events": 0}

    def emit(messages, checkpoint=False):
        """Packetize one event and take a book key checkpoint every N
        events, or now if asked."""
        for m in messages:
            tag = len(published)
            published.append(m)
            packetizer.add(m.time_ns, m.channel, m.data, tag)
            last_tag_on_channel[m.channel] = tag
        state["events"] += 1
        if checkpoint or state["events"] % config.book_key_every == 0:
            # A correct client holds an empty book between a ChannelReset
            # and the instrument's own rebuild.
            books = {i.name: (last_tag_on_channel[i.channel],
                              ((), ()) if i.name in cleared else engine.books[i.name].top(5))
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

    on_channel = {c: [i.name for i in insts if i.channel == c] for c in channels}
    reset_log = []

    def rebuild(t, names, log):
        """One recovery event per instrument with a book, each followed by a
        book key checkpoint."""
        for name in names:
            orders = engine.books[name].resting_orders()
            if not orders:
                continue  # only instruments that had a book (Guidelines p.46)
            first = len(published)
            cleared.discard(name)
            emit(publisher.rebuild(t, name, orders), checkpoint=True)
            log["rebuild_tags"][name] = (first, len(published) - 1)

    def sequence_reset(t, channel, log):
        m = publisher.sequence_reset(t, channel)
        tag = len(published)
        published.append(m)
        packetizer.bump_version(t, channel, final_message=m.data, final_tag=tag)
        last_tag_on_channel[channel] = tag
        log["sequence_reset_tags"][channel] = tag

    def next_session(t, days, log, at_start=None):
        """End the session at t, stream down until START_OF_DAY_NS before
        the session `days` later, call at_start (the weekly start-up) and
        open the new session with the same bands."""
        for m in publisher.end_session(t):
            emit([m])
        start = config.start_ns + (log["trade_date_before"] - config.trade_date + days) * DAY_NS
        for c in channels:
            packetizer.stream_down(t, c, start - START_OF_DAY_NS)
        flow.pause(start - t)
        if at_start:
            at_start(start)
        new_date = log["trade_date_before"] + days
        new_values = {}
        for i in insts:
            close = publisher.closing_price(i.name)
            low, high = _band(close, config.band_bps)
            new_values[i.name] = dataclasses.replace(
                values[i.name], prev_close=close, low_limit=low, high_limit=high,
                settlement_price=close if i.is_future else None,
                settl_date=log["trade_date_before"] if i.is_future else None)
            flow.set_band(i.name, low, high)
        values.update(new_values)
        log.setdefault("bands", {}).update({n: [v.low_limit, v.high_limit]
                                            for n, v in new_values.items()})
        session = publisher.start_session(start, new_date, new_values)
        for k, m in enumerate(session):
            emit([m], checkpoint=k == len(session) - 1)
        log["trade_date_after"] = new_date
        log["session_start_ns"] = start
        return start

    def apply_reset(reset, t):
        log = {"kind": reset.kind, "time_ns": t, "target": reset.target,
               "sequence_reset_tags": {}, "rebuild_tags": {}}
        first = len(published)
        if reset.kind == "failover":
            for c in channels:
                sequence_reset(t, c, log)
            for c in channels:
                rebuild(t, on_channel[c], log)
        elif reset.kind == "channel_reset":
            cleared.update(on_channel[reset.target])
            emit(publisher.channel_reset(t, reset.target))
            rebuild(t, on_channel[reset.target], log)
            cleared.clear()  # instruments without a book stay empty anyway
        elif reset.kind == "instrument_reset":
            rebuild(t, [reset.target], log)
        else:
            log["trade_date_before"] = publisher.trade_date(insts[0].name)

            def start_up(start):
                for c in channels:
                    sequence_reset(start, c, log)
                for c in channels:
                    cleared.update(on_channel[c])
                    emit(publisher.channel_reset(start, c))
                for c in channels:
                    rebuild(start, on_channel[c], log)
                cleared.clear()

            weekly = reset.kind == "weekly_start"
            t = next_session(t, 3 if weekly else 1, log, start_up if weekly else None)
        log["tags"] = list(range(first, len(published)))
        reset_log.append(log)
        return t

    injection_log = []
    pending = []  # heap of (after_messages, order, action)
    order = itertools.count()
    for item in (*config.resets, *config.injections):
        heapq.heappush(pending, (item.after_messages, next(order), item))
    modify = {"extend": 0, "unknown": None}  # applied to the next live events
    news_rng = random.Random(f"{config.seed}:news")

    def submit(name, instruction):
        instructions[type(instruction).__name__] += 1
        outcome = engine.submit(name, instruction)
        outcomes[type(outcome).__name__] += 1
        flow.on_outcome(outcome)
        return outcome

    def inject(item, t):
        args = item.params
        log = {"kind": item.kind, "time_ns": t, **{k: v for k, v in args.items()}}
        first = len(published)
        if item.kind == "mass_cancel":
            emit(publisher.publish(submit(args["instrument"],
                                          eng.MassCancel(t, args["side"]))))
        elif item.kind == "sweep":
            opposite = eng.SELL if args["side"] == eng.BUY else eng.BUY
            book = [o for o in engine.books[args["instrument"]].resting_orders()
                    if o.side == opposite][:args["orders"]]
            log["orders_hit"] = len(book)
            emit(publisher.publish(submit(args["instrument"], eng.MarketOrder(
                t, args["side"], sum(o.qty for o in book), firm=1))))
        elif item.kind == "trade_bust":
            trade = next(m for m in reversed(published) if m.template_id == T_TRADE
                         and m.instrument == args["instrument"])
            log["trade_tags"] = [published.index(trade)]
            emit(publisher.trade_bust(t, trade))
        elif item.kind == "news":
            news_id = len(injection_log) + 1
            parts = publisher.news_parts(
                t, args["channel"], args["instrument"], news_id,
                f"Aviso {news_id}", _news_text(news_rng, args["text_bytes"]),
                args["part_size"], block_extra=args.get("block_extra", 0),
                version=args.get("version"))
            log.update(newsID=news_id, part_count=len(parts), part_tags=[])
            for k, part in enumerate(parts):
                if k and args["spread"]:
                    # Later parts go out after live traffic, at their own time.
                    heapq.heappush(pending, (len(published) + k * args["spread"],
                                             next(order), ("news_part", part, log)))
                else:
                    log["part_tags"].append(len(published))
                    emit(part)
        elif item.kind == "extend":
            modify["extend"] = args["count"]
            log["event_tags"] = []
        elif item.kind == "unknown":
            body = bytes(news_rng.randrange(256) for _ in range(args["body_bytes"]))
            modify["unknown"] = (args["template_id"], body, log)
        if item.kind not in ("extend", "unknown", "news"):
            log["event_tags"] = list(range(first, len(published)))
        if item.kind == "extend":
            modify["extend_log"] = log
        injection_log.append(log)

    def news_part(part, log, t):
        log["part_tags"].append(len(published))
        # Sent when its turn comes: only the send time moves (origTime and
        # the bytes stay the News's own).
        emit([dataclasses.replace(m, time_ns=t) for m in part])

    def publish_live(outcome, t):
        kind = type(outcome)
        if modify["extend"] and kind is eng.Rested:
            modify["extend"] -= 1
            modify["extend_log"]["event_tags"].append(len(published))
            return publisher.publish(outcome, block_extra=EXTENDED_BLOCK,
                                     version=EXTENDED_VERSION)
        messages = publisher.publish(outcome)
        if modify["unknown"] and len(messages) >= 2:
            template_id, body, log = modify["unknown"]
            modify["unknown"] = None
            unknown = Publisher.unknown(messages[0].time_ns, messages[0].channel,
                                        template_id, body, EXTENDED_VERSION)
            log["message_tags"] = [len(published) + 1]
            return [messages[0], unknown, *messages[1:]]
        return messages

    outcomes = Counter()
    instructions = Counter()
    streak = {i.name: 0 for i in insts}
    max_streak = {i.name: 0 for i in insts}
    arrivals = []
    t = config.start_ns
    paused = config.idle is None
    while len(published) < config.target_messages:
        while pending and len(published) >= pending[0][0]:
            item = heapq.heappop(pending)[2]
            if isinstance(item, scenarios.Reset):
                t = apply_reset(item, t)
            elif isinstance(item, tuple):
                news_part(item[1], item[2], t)
            else:
                inject(item, t)
        if not paused and len(published) >= config.idle.after_messages:
            # Shift the arrival clock before the engine, so every time on
            # the wire (transactTime, insert times, sendingTime) moves together.
            flow.pause(config.idle.duration_ns)
            paused = True
        arrival = flow.next_arrival()
        t = arrival.time_ns
        name = arrival.instrument
        top = engine.books[name].top(5)
        streak[name] = streak[name] + 1 if not (top[0] and top[1]) else 0
        max_streak[name] = max(max_streak[name], streak[name])
        instruction = flow.decide(arrival, top)
        if config.record:
            arrivals.append((arrival, instruction))
        outcome = submit(name, instruction)
        messages = publish_live(outcome, t)
        if messages:
            emit(messages)
    for m in publisher.end_session(t):
        emit([m])

    packetized = packetizer.finish(t)
    out = Path(config.out_dir)
    result = RunResult(
        pcap_path=out / "feedA.pcap", intent_path=out / "intent.jsonl",
        book_key_path=out / "book_key.jsonl", messages=len(published),
        events=state["events"], packets=len(packetized.packets),
        heartbeats=len(packetized.heartbeats), outcomes=outcomes, instructions=instructions,
        max_empty_side_streak=max_streak,
        final_resting={i.name: len(engine.books[i.name].resting_orders()) for i in insts},
        arrivals=arrivals,
        regimes=list(flow.clock.segments) if config.record else [],
        message_times=[(m.channel, m.time_ns) for m in published] if config.record else [],
        resets=reset_log, injections=injection_log)
    return result, published, packetized, checkpoints


def _write(config, result, published, packetized, checkpoints, feed_a):
    """Feed A (as given), intent log and book key: the key files always
    describe everything published, whatever a feed lost."""
    Path(config.out_dir).mkdir(parents=True, exist_ok=True)
    pcap.write_pcap(result.pcap_path, feed_a, channel_map=config.channel_map,
                    src_ip=config.src_ip, src_port=config.src_port)
    golden.write_jsonl(result.intent_path, golden.intent_rows(published, packetized))
    golden.write_jsonl(result.book_key_path, golden.book_key_rows(checkpoints, packetized))
