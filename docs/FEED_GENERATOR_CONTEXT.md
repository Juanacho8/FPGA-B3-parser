# Feed Generator: Build Context

Brief for the chat or agent building the B3 feed generator. Read this first, then:

* `spec/LAYOUTS.md`: B3's wire format and feed behavior (byte layouts for every message, verified against B3's XML and PDFs). Every byte the generator writes must match it.
* `docs/PROJECT_CONTEXT.md`: whole-project context (C++ and RTL phases, C6 audience, overall plan).
* `spec/b3-market-data-messages-2.3.1.xml`: B3's official schema. Source of truth when anything disagrees.

---

## 1. What we are building and why

A Python program that **pretends to be B3's market data system**. It produces the exact UDP packets B3 would multicast on the Binary UMDF incremental stream, written as pcap files, plus answer keys describing what those packets mean.

Purpose:

* **Build and test tool.** It produces traffic to develop and verify a C++ parser and an FPGA (RTL) parser.
* **Relative timing only.** It may be used to compare timing between implementations, never for absolute performance claims about B3.
* **Fidelity to B3 is the bar.** A parser that works on our pcaps should work on a real B3 capture. When in doubt, follow `spec/LAYOUTS.md`, and B3's XML above all.

## 2. Working rules

* **Only touch `feed_generator/`** (package `b3gen`), plus the root `.gitignore` and `spec/`. Never touch `cpp_baseline/`, `rtl_parser/` or `README.md`.
* **Python standard library only.** No scapy, numpy or other dependencies. pytest for tests.
* **Build one step at a time** (section 11). Stop at each step's "done when" check before moving on.
* **Answer keys come from intent, never from decoding our own bytes.** The intent log is written from the publisher's own data. If it were produced by decoding the pcap, an encoder bug would hide itself.
* **Deterministic.** Same seed and config gives byte-identical output.
* Gabriel builds with Claude Code and wants to understand each piece well enough to explain it. Prefer clear code over clever code.

## 3. Scope

| In v1 | Not in v1 |
|---|---|
| Incremental stream, Feed A and Feed B | Snapshot recovery stream, instrument definition stream (v2) |
| Continuous trading | Opening and closing auctions |
| B3 restart and reset behavior (section 9) | Forward ("termo") instruments |
| Statistics B3 sends during continuous trading (section 6) | Implied orders, iceberg orders, self-trade prevention |

## 4. Architecture

Five layers, each feeding the next. **The order flow layer and the matching engine are separate**, so the order model can later be upgraded or replaced with real order data without touching anything else.

```
order flow -> matching engine -> market data publisher -> packetizer -> wire writer
 (intents)     (book + fills)     (B3 messages + keys)    (packets)     (pcap)
```

| Layer | Module | Responsibility | Must not |
|---|---|---|---|
| 1. Order flow | `flow.py` | Timestamped instructions: new limit order, cancel, size change, price change, aggressive order | Know about the book's internals or B3 messages |
| 2. Matching engine | `engine.py` | One book per instrument. Price/priority, matching, returns outcomes | Know about B3 messages, rptSeq, packets |
| 3. Publisher | `publisher.py` | Turns outcomes into B3 messages (section 5). Owns rptSeq, matchEventIndicator, statistics state. Writes the intent log | Know about packet boundaries |
| 4. Packetizer | `packet.py` | Groups messages into packets (section 7), packet headers, heartbeats, Feed A and B | Change message content |
| 5. Wire writer | `pcap.py` | Ethernet/IPv4/UDP framing, pcap files | |
| Support | `layouts.py`, `encode.py` | Field offsets, enums and sets (generated, item B), one encoder per template | |
| Support | `scenarios.py`, `config.py`, `main.py` | Scenario injection, config file, CLI | |

The original skeleton named the order flow and engine together as `sim.py`. Splitting it into `flow.py` and `engine.py` is the decided structure. `golden.py` from the skeleton becomes the intent log and book key writer.

## 5. Matching engine and publisher rules

### Engine

* Book per instrument and side. Bids by price high to low, offers low to high. Within a price, by **secondaryOrderID ascending** (smaller is first in line).
* **secondaryOrderID** comes from one increasing counter per market segment (equities and derivatives each have their own). It changes whenever an order loses priority: size increase or price change.
* An aggressive order matches against the opposite side best price first, then by priority, until filled or no longer crossing. Any remainder of a limit order rests at its limit price. Any remainder of a market or immediate-or-cancel order is discarded, and that produces no message.

### Publisher: outcome to B3 messages

Every row below is **one event**. Only the **last message of the event** gets EndOfEvent (bit 7, 0x80) in matchEventIndicator.

| Engine outcome | Messages, in order |
|---|---|
| Limit order rests (no cross) | Order_MBO NEW (new secondaryOrderID) |
| Cancel | DeleteOrder_MBO (remaining size, price, its secondaryOrderID) |
| Size reduction (keeps priority) | Order_MBO CHANGE (same secondaryOrderID, new size, mDEntryPrevSize = old size) |
| Size increase or price change (loses priority) | DeleteOrder_MBO for the old order, then Order_MBO NEW with a new secondaryOrderID |
| Aggressive order with fills | 1. ExecutionSummary. 2. Trade, one per fill. 3. Per resting order hit: DeleteOrder_MBO if fully filled, Order_MBO CHANGE (with prevSize) if partially filled. 4. Order_MBO NEW for the aggressor's remainder if it rests. 5. OpeningPrice if this is the session's first trade, HighPrice / LowPrice if a new session extreme. 6. ExecutionStatistics |
| Mass cancel of one side (scenario) | MassDeleteOrders_MBO, DELETE_THRU |

Field rules:

* **ExecutionSummary**: aggressorSide, lastPx = price of the last fill, fillQty = total filled, cxlQty and tradedHiddenQty null, aggressorTime and transactTime. It has no matchEventIndicator field.
* **Trade**: price, size, tradeID (counter per instrument per trading day), mDEntryBuyer and mDEntrySeller firms, tradingSessionID = 1 (regular). tradeCondition: RegularTrade (bit 13) always. OpeningPrice (bit 0) on the session's first trade. LastTradeAtTheSamePrice (bit 2) when equal to the previous trade price.
* **Order_MBO**: mDInsertTimestamp = when the order entered or re-entered the book. transactTime = when this event happened. enteringFirm set. mDEntryPrevSize null except on CHANGE.
* **DeleteOrder_MBO**: mDEntrySize = remaining size just before deletion. mDEntryPx = the order's price.
* **Deprecated fields** (e.g. SettlementPrice openCloseSettlFlag, deprecated in schema 17) still occupy their bytes on the wire. The encoder writes them as null, never skips them.
* **rptSeq**: per instrument, +1 on every message that carries a securityID and rptSeq field. Restarts at 1 after an EmptyBook for that instrument.
* **Timestamps**: one simulated nanosecond clock. Within an event, transactTime is the same for all messages. sendingTime (packet header) is at or after the transactTime of every message in the packet.

### Statistics state per instrument

* tradeVolume and numberOfTrades accumulate per session.
* vwapPx = Σ(price × qty) / Σ(qty), in 10⁻⁴ units. B3 does not document rounding. **Assumption: round half to even.** Flag it if it matters later.
* netChgPrevDay = last trade price minus the previous close, in 10⁻⁸ units.
* Session high and low track trade prices. OpeningPrice is sent once, on the first trade.
* All of it resets at a new trading session (section 9).

## 6. Messages emitted

| Template | When |
|---|---|
| 50 Order_MBO, 51 DeleteOrder_MBO | Every book change |
| 52 MassDeleteOrders_MBO | Scenario |
| 55 ExecutionSummary, 53 Trade, 56 ExecutionStatistics | Every matching event |
| 15 OpeningPrice | First trade of the session |
| 24 HighPrice, 25 LowPrice | New session high or low |
| 17 ClosingPrice | Session start (previous close) and session end |
| 22 PriceBand, 21 QuantityBand | Session start |
| 28 SettlementPrice, 29 OpenInterest | Session start, futures only |
| 10 SecurityGroupPhase, 3 SecurityStatus | Session start and status changes |
| 2 Sequence | Heartbeat (section 7) |
| 1 SequenceReset, 9 EmptyBook, 11 ChannelReset | Restart and reset scenarios |
| 57 TradeBust | Scenario |
| 5 News | Scenario. Exercises variable-length data and multi-part messages |

Statistics and status messages outside a matching event (session start, session end) **each carry EndOfEvent on their own**, as B3 specifies.

Not emitted for now: LastTradePrice (mostly a snapshot message), TheoreticalOpeningPrice and AuctionImbalance (auctions), ForwardTrade, SecurityDefinition on the incremental stream.

**Session start sequence (our inference, B3 does not specify it):** SecurityGroupPhase OPEN for each security group, then per instrument ClosingPrice (previous close), PriceBand, QuantityBand, and SettlementPrice + OpenInterest for futures. Then continuous trading begins.

## 7. Packetizer

### Packetization: option 3 (decided)

* Close the current packet when **the next message would push it past 1400 bytes**, or when **a time window has passed since the packet's first message**. Default window 5 µs, configurable.
* Window 0 gives roughly one message per packet. An infinite window gives "always fill". Both remain available for stress tests.
* The result naturally includes packets with several events and events spanning packets, as B3 documents.
* B3 does not publish its batching rule. This is our inference, to be tuned against a real capture if one becomes available. Keep the policy a swappable setting.

### Packet header and sequencing

* Header: channelNumber, reserved 0, sequenceVersion, sequenceNumber, sendingTime (ns).
* sequenceNumber +1 per packet within a channel and sequenceVersion. sequenceVersion starts at 1 and is incremented by restart scenarios. When it changes, sequenceNumber restarts at 1.
* **Heartbeat:** when a channel is idle more than 1 s, send a packet containing only a Sequence message, with packet **sequenceNumber 0** and nextSeqNo = the sequenceNumber the next real packet will have. Repeat every 1 s while idle.

### Feeds A and B

* Two separate pcaps: `feedA.pcap`, `feedB.pcap`.
* Feed B carries the same packets, byte for byte, with a small extra delay. Per B3 circular OC 131/2023, Feed A is the faster one.
* In loss scenarios, Feed A drops some packets that Feed B still carries.

## 8. Order flow and timing

* **Zero-intelligence model:** random limit orders placed near the mid price (a few ticks either side), random cancels, size changes and price changes of resting orders, occasional aggressive orders that cross the spread. Order sizes from a simple distribution in lot multiples.
* **Bursty timing (decided, build now):** arrivals follow a Poisson process whose rate switches between a calm regime and a busy regime, with random regime durations. Uniform timing would understate parser stress.
* Keep the book populated: if a side gets thin, bias new orders toward it.
* **Regimes are market-wide** (decided in step 6): one `random.Random(f"{seed}:regime")` decides only the calm/busy switches. Each instrument keeps its own `random.Random(f"{seed}:{name}")` for arrivals and decisions, and its own arrival rate in each regime.
* **Spike regime** (decided as a fix to step 6): a third, short regime (mean 1 ms) arriving as its own Poisson process (mean one per second of calm/busy time) that interrupts either regime, which then resumes. Spike rates are ~1.6M arrivals/s per channel, because filling 1400 B inside the 5 µs window takes ~15 messages; at ~200k/s per channel only 2 packets in the default 20k file closed by size. In that file (seed 1) the spike lasts 0.84 ms and gives 84 packets closed by the size limit and 42 events spanning two packets. Spikes carry ~20% of its arrivals.
* **Cancels scale with the book** (decided in step 6): the cancel weight is `p_cancel × live orders / target_live_orders` (default 200 per instrument), so each order has a roughly constant chance of being cancelled and the book settles at a stable size. With a flat cancel rate the book grew for the whole run, which was unrealistic and made run time superlinear.
* A midpoint between two ticks rounds up or down using the instrument's RNG, so prices do not drift one way.
* Fancier models are deferred. The layer separation is what makes that a later drop-in change.

## 9. Restart and reset scenarios

| Scenario | What the generator emits |
|---|---|
| Weekly start | sequenceVersion +1, sequenceNumber 1, ChannelReset, then books rebuilt with recovery orders |
| Engine failover | SequenceReset, sequenceVersion +1, then per instrument with a book: EmptyBook, then Order_MBO NEW for every resting order with RecoveryMsg (bit 5, 0x20) set. Last message of each instrument's rebuild gets EndOfEvent (0xA0 combined) |
| ChannelReset | ChannelReset, then the same per-instrument rebuild |
| Single-instrument reset | EmptyBook + recovery orders for one instrument. Its rptSeq restarts at 1 |
| New trading session | SecurityGroupPhase with securityTradingEvent = TRADING_SESSION_CHANGE (4). Statistics reset, tradeID restarts, new tradeDate |

The engine always knows the true book, so a rebuild republishes the true book with the orders' existing secondaryOrderIDs. The answer keys then show exactly what a correct client holds afterwards.

Other scenarios, each in its own file, separate from the clean main file:

| Scenario | Content |
|---|---|
| Loss | Feed A drops packets that Feed B carries |
| Gaps | Packets missing from both feeds |
| Idle | Quiet periods with heartbeats |
| Edge orders | Null-price (market-on-auction style) orders, MassDelete, aggressions sweeping several levels, events spanning packets |
| Schema evolution | A message with a larger blockLength than the schema (extra zero bytes appended), and an unknown templateId |
| Rare messages | TradeBust, multi-part News |
| Malformed | Truncated packets, bad messageLength. Small separate file |

## 10. Config and outputs

### Config (one file)

| Instrument | Type | Segment | Tick | Channel |
|---|---|---|---|---|
| WIN | Mini Ibovespa future | Derivatives | 5 points | 68 |
| DOL | US dollar future | Derivatives | 0.5 | 68 |
| PETR4 | Equity | Equities | 0.01 | 70 |
| VALE3 | Equity | Equities | 0.01 | 70 |

* Channel numbers are real B3 Binary UMDF channels. securityIDs, starting prices, multicast addresses and ports are invented.
* All of it lives in the config file, so B3's real Market Data Channels Definition can be swapped in later.
* Also in config: seed, message count, packet window, heartbeat interval, burst regime parameters, Feed B delay, loss rates.

### Outputs

| File | Content |
|---|---|
| `feedA.pcap`, `feedB.pcap` | Nanosecond-resolution pcap. Ethernet + IPv4 + UDP to the channel's multicast group. IP/UDP headers big-endian, B3 payload little-endian |
| Intent log | One row per message: feed packet sequenceNumber, message index within the packet, templateId, and every field value as the publisher intended. JSON Lines is suggested |
| Book key | True book, top 5 levels per side per instrument, at checkpoints (e.g. every 1,000 events) |

The parser output format (decision A) is deferred to the C++/RTL phase. A small script will convert the intent log into it later.

Standard runs: small (~20k messages) for RTL simulation, large (~1M+) for C++ runs, a burst-heavy file, and the scenario files.

## 11. Build order

| # | Step | Done when |
|---|---|---|
| 0 | Skeleton inside `feed_generator/` | pytest runs |
| 1 | `layouts.py`, generated by `tools/gen_layouts.py` (item B) | Matches `spec/LAYOUTS.md` for every template in section 6 |
| 2 | Encoders | B3's sample packet (Message Reference section 6: Order_50 + Trade_53, 168 bytes) re-encodes byte for byte from its decoded field values |
| 3 | Packetizer (option 3) + pcap writer | Opens in Wireshark. Packets at most 1400 B. Sequencing and heartbeat rules hold |
| 4 | Matching engine alone | Unit tests: priority ordering, partial fills, sweeps across levels, cancels, requeue on size increase and price change |
| 5 | Publisher | Each outcome in section 5 produces exactly its message pattern. EndOfEvent only on the last message. rptSeq and statistics correct |
| 6 | Order flow with bursty timing + intent log + book key | Same seed gives byte-identical output |
| 7 | Feed B and scenarios | Each scenario appears where expected |
| 8 | CLI and config file | Small, large, burst and scenario files from one command |

**Verification ideas worth building in:**
* A tiny independent decoder in the tests (not used to produce answer keys) that walks every packet using only messageLength. It checks framing, lengths and sequence rules.
* Book invariants after every event: no crossed book, no negative sizes, every delete refers to a resting order.

## 12. Open items

| # | Item | Status |
|---|---|---|
| A | Parser output contract | Deferred to C++/RTL phase |
| B | `layouts.py`: generated or hand-written | **Decided.** Generated at build time by `feed_generator/tools/gen_layouts.py` from `spec/b3-market-data-messages-2.3.1.xml` (SHA-256 checked before generating), output checked in as `b3gen/layouts.py`. Tests fail if the checked-in file differs from a fresh run, and check every template's blockLength and offsets against `spec/LAYOUTS.md`. The same generator can later emit a C++ header and an SV package |
| C | Diff Guidelines v2.3.1.2 against v2.3.1 | Open |
| D | VWAP rounding rule | Assumed round half to even |
| E | Session start message order | Inferred (section 6) |
| F | Packetization window value | 5 µs default, unvalidated until a real capture exists |
| G | Crossing price change message order | Inferred: ExecutionSummary, then DeleteOrder_MBO for the old order (old secondaryOrderID, price, size), then the normal execution sequence (Trades, resting order updates, the remainder as Order_MBO NEW with its new secondaryOrderID, statistics). B3 does not document a modify that crosses |
| H | When OpeningPrice, HighPrice and LowPrice are sent | Inferred: on the session's first trade, OpeningPrice, HighPrice and LowPrice all go out. In later matching events, HighPrice and/or LowPrice only when the session extreme changed, once per event with its final value (not once per fill) |
| I | Session start and end values | Inferred. Order: SecurityGroupPhase (OPEN, securityTradingEvent TRADING_SESSION_CHANGE) for each security group first, because clients clear statistics when they see it (Guidelines 6.5.12 note and 14.3), so the new session's ClosingPrice and bands must come after it. Then per instrument: ClosingPrice with openCloseSettlFlag ENTRY_FROM_PREVIOUS_BUSINESS_DAY, PriceBand as HARD_LIMIT in PRICE_UNIT, QuantityBand, and for futures SettlementPrice (settlPriceType FINAL, priceType PU, settlDate = previous day, per Guidelines 13.11.3 "before trading session: D-1 FINAL") and OpenInterest. OpeningPrice uses openCloseSettlFlag DAILY. Session end: ClosingPrice with DAILY at the last trade price, or the previous close if the instrument did not trade. A mass cancel that removes no orders publishes nothing |
