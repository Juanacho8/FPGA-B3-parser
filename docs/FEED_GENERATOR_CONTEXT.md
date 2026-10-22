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
* **secondaryOrderID** is unique across all market segments and incremental per segment (Guidelines p.68). Each segment has its own counter inside its own range of 10¹² IDs: the first segment in the config counts from 1, the second from 10¹² + 1, so IDs never repeat across segments. It changes whenever an order loses priority: size increase or price change.
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
| Market supervision deletes one side (scenario, Guidelines p.71) | MassDeleteOrders_MBO, DELETE_THRU |
| A firm's mass cancel (scenario, Guidelines p.15) | One DeleteOrder_MBO per resting order of the firm, across the instruments of one channel, as one event (open item P) |

Field rules:

* **ExecutionSummary**: aggressorSide, lastPx = price of the last fill, fillQty = total filled, tradedHiddenQty = 0 (no hidden orders in v1), cxlQty = the market/IOC remainder discarded at the end of matching, 0 when nothing is cancelled (open item N), aggressorTime and transactTime. B3's examples show 0, not null, for these quantities. It has no matchEventIndicator field.
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
| 10 SecurityGroupPhase | Session start (TRADING_SESSION_CHANGE) |
| 2 Sequence | Heartbeat (section 7) |
| 1 SequenceReset, 9 EmptyBook, 11 ChannelReset | Restart and reset scenarios |
| 57 TradeBust | Scenario |
| 5 News | Scenario. Exercises variable-length data and multi-part messages |

Statistics and status messages outside a matching event (session start, session end) **each carry EndOfEvent on their own**, as B3 specifies.

Not emitted for now: SecurityStatus (v1 has no per-instrument status changes: every instrument follows its group's phase), LastTradePrice (mostly a snapshot message), TheoreticalOpeningPrice and AuctionImbalance (auctions), ForwardTrade, SecurityDefinition on the incremental stream.

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
* **Feed B timing** (decided in step 7a): B time = A time + base delay (default 20 µs) + uniform jitter in [0, 10 µs] from `random.Random(f"{seed}:feedB")`, clamped so B never reorders a channel. Only the pcap timestamp moves: the header sendingTime stays Feed A's, byte for byte. B has its own multicast groups (`channel_map_b`).

## 8. Order flow and timing

* **Zero-intelligence model:** random limit orders placed near the mid price (a few ticks either side), random cancels, size changes and price changes of resting orders, occasional aggressive orders that cross the spread. Order sizes from a simple distribution in lot multiples.
* **Bursty timing (decided, build now):** arrivals follow a Poisson process whose rate switches between a calm regime and a busy regime, with random regime durations. Uniform timing would understate parser stress.
* Keep the book populated: if a side gets thin, bias new orders toward it.
* **Regimes are market-wide** (decided in step 6): one `random.Random(f"{seed}:regime")` decides only the calm/busy switches. Each instrument keeps its own `random.Random(f"{seed}:{name}")` for arrivals and decisions, and its own arrival rate in each regime.
* **Spike regime** (decided as a fix to step 6; **a stress setting, not B3's real rate**): a third, short regime (mean 1 ms) arriving as its own Poisson process (mean one per second of calm/busy time) that interrupts either regime, which then resumes. Spike rates are ~1.6M arrivals/s per channel, because filling 1400 B inside the 5 µs window takes ~15 messages; at ~200k/s per channel only 2 packets in the default 20k file closed by size. In that file (seed 1) the spike lasts 0.84 ms and gives 84 packets closed by the size limit and 50 events spanning two packets (counting packets that end in an ExecutionSummary, which has no matchEventIndicator and is always followed by its trades). Spikes carry ~23% of its arrivals.
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
| Edge orders | Market supervision deleting a side (DELETE_THRU), a firm's mass cancel across instruments, a sweep across several levels, events spanning packets. Null-price orders are out of v1 (open item K) |
| Schema evolution | Messages with a larger blockLength than the schema (extra zero bytes appended) and of older versions (shorter block, later fields absent), and an unknown templateId |
| Rare messages | TradeBust, multi-part News |
| Malformed | Truncated packets, bad messageLength. Small separate file |

Step 7a decisions (loss, gaps, idle):

* `main.run_scenario(config, name)` writes `out_dir/<name>/` with `feedA.pcap`, `feedB.pcap`, `intent.jsonl`, `book_key.jsonl` and `manifest.json`, for `clean`, `loss`, `gap` and `idle`. All four run the same session, so the clean Feed A is the full feed.
* **Burst model** for loss (Feed A only) and gaps (both feeds): at each sequenced packet of a channel a burst starts with probability `burst_rate`, length geometric with mean `mean_burst`. Defaults: loss 1% / 3, gaps 0.1% / 2. Own RNGs `f"{seed}:loss"` and `f"{seed}:gap"`. Heartbeats are never dropped (sequenceNumber 0 cannot be named in a manifest).
* **Idle:** the flow's arrival clock is paused (`OrderFlow.pause`) before the engine, so transactTime, insert times and sendingTime all move together; nothing already encoded is shifted. Default: once half the target messages are published, 3.5 s, giving 3 heartbeats per channel on both feeds.
* **Manifest:** `missing` lists `[channel, sequenceVersion, sequenceNumber]` missing from each feed; `unreliable_books` maps each instrument to the first packet missing from both feeds that held one of its messages. A message with no instrument (SecurityGroupPhase) makes every instrument on its channel unreliable from that packet. A book stays unreliable until a snapshot (v2).
* The intent log and book key always describe everything published, whatever a feed lost.

Step 7b decisions (restart and reset scenarios, `run_scenario` kinds `failover`, `channel_reset`, `instrument_reset`, `session_change`, `weekly_start`; each at half the target message count):

* **SequenceReset** goes alone in the old version's last packet (sequenceNumber X+1, version N); the next packet is version N+1, sequenceNumber 1 (LAYOUTS.md 12.1, from the Guidelines p.45 diagram).
* **matchEventIndicator:** ChannelReset is its own event, 0xA0 (Guidelines p.47 draws it outside the recovery bracket). Each instrument's rebuild is a separate event: EmptyBook 0x20, Order_MBO NEW 0x20, the last 0xA0. Only instruments with resting orders are rebuilt ("only for instruments that previously had a book", Guidelines p.46).
* **Rebuilds** republish the engine's resting orders in priority order (bids, then offers) with their secondaryOrderID and original mDInsertTimestamp; transactTime is the rebuild time. rptSeq restarts at 1 on the message after EmptyBook.
* **Statistics** survive failover and resets; only TRADING_SESSION_CHANGE clears them, with tradeID and tradeDate (LAYOUTS.md 12.9 and section 13).
* **Book key:** between a ChannelReset and an instrument's own rebuild, the key shows an empty book for it, which is what a correct client holds. A checkpoint is taken after every rebuild and after each new session's start.
* **Failover:** SequenceReset on every channel, then rebuilds; no ChannelReset.
* **channel_reset:** one channel, no version change.
* **session_change:** end of session, stream down overnight (no heartbeats), 3 start-of-day heartbeats, next day's session (TRADING_SESSION_CHANGE first). Orders carry over. The new prev_close (and futures' settlement) is the day's close, and the PriceBand is recomputed around it (`band_bps`, default ±8%). The flow then keeps new prices inside the new band; an order resting outside it can only be reduced or cancelled, since an increase would requeue it outside the band.
* **weekly_start:** the file opens on Friday. End of session, stream down over the weekend, 3 start-of-day heartbeats (nextSeqNo = the SequenceReset packet's number), SequenceReset and version +1 per channel, ChannelReset per channel, rebuilds of the orders carried over, then Monday's session. No SecurityDefinition resend (v2).
* The manifest's `resets` entry gives kind, time, target, trade dates, the first and last placements, each channel's SequenceReset placement and each instrument's rebuild span.

Step 7c decisions (`edge`, `rare`, `schema`, `malformed`; injections at half the target message count):

* **edge:** a market-supervision SupervisorDeleteThru of each side of PETR4 (MassDeleteOrders_MBO DELETE_THRU, each its own 0x80 event); a firm mass cancel on channel 68 (every resting WIN and DOL order of the firm with the most orders there, one event of DeleteOrder_MBO, open item P); and a market order for the best 30 offers on WIN, whose event spans 3+ packets. The flow drops the handles these removed; the run has no Rejected outcomes.
* **rare:** a TradeBust copying WIN's latest Trade (open item L); a single-part News for WIN and a 3-part market-wide News on channel 70 (open item M). Each part is over 700 B so no two share a packet, and live traffic goes out between parts (40 messages apart).
* **Gap rule narrowed:** only SecurityGroupPhase, ChannelReset and SequenceReset make a whole channel unreliable; News and unknown templates never make a book unreliable.
* **schema:** three live Order_MBO NEW and one News with blockLength = schema + 8 (zero bytes, var data after the larger block) and header version 18; four live Order_MBO NEW of older versions (two at version 15: no mDEntryPrevSize, blockLength 64; two at version 14: no transactTime either, blockLength 56), whose intent rows list those fields as "absent"; and a templateId 999 message with a 20-byte random body inside a live event (a real message before and after it in the packet). Clients decode the known fields with the header's blockLength, skip the rest, and skip unknown templates by messageLength.
* **malformed:** a 1,500-message run with one fault in each of five packets (messageLength past the end, 0, under 12, a wrong encodingType, the datagram cut inside the last message's header) and a stray 10-byte datagram that uses up no sequence number. The manifest lists each fault's packet, kind, byte offset and messages before it. Clients process the complete messages before the fault, discard the rest of the packet and carry on with sequencing; instruments whose book messages were discarded are listed as unreliable from that packet.

## 10. Config and outputs

### Config (one file)

| Instrument | Type | Segment | Tick | Channel |
|---|---|---|---|---|
| WIN | Mini Ibovespa future | Derivatives | 5 points | 68 |
| DOL | US dollar future | Derivatives | 0.5 | 68 |
| PETR4 | Equity | Equities | 0.01 | 70 |
| VALE3 | Equity | Equities | 0.01 | 70 |

* securityIDs, starting prices, multicast addresses and ports are invented, and so is which channel carries which instrument (B3's real assignment is in its Market Data Channels Definition, not in spec/).
* All of it lives in the config file (`feed_generator/config.toml`, decided in step 8), so B3's real channel definitions can be swapped in later.
* Also in config: seed, message targets per set, packet window, heartbeat interval, regime and flow parameters (spike included), Feed B delay and jitter, loss/gap/idle parameters, session date.
* **Config file (step 8):** TOML, read with `tomllib`. Every value carries a comment labelling it [B3] (from spec/), [B3 market] (a real convention not in spec/, such as ticks and lots), [inferred], [invented] or [stress]. Validation is strict: unknown keys, missing keys, wrong types and inconsistent values raise with the key path. The default file gives exactly `main.default_config()`.
* **CLI (step 8):** `python -m b3gen build` builds `small`, `large`, `burst` (spike_every 50 ms, 200k messages, about 100 spikes) and every scenario folder from one config; `--seed` and `--messages` override every set chosen (malformed keeps 1,500). Every folder has both feeds, the keys, a manifest and `provenance.json` (config text and SHA-256, seed, git commit and dirty flag, schema XML SHA-256, command line). A scenario that cannot place what it promises within its target fails with a clear error.
* **Streaming (step 8):** the packetizer hands over each packet once no channel can still produce an earlier one; Feed A's pcap and the intent log are written then, and the messages forgotten. Output is byte-identical to the in-memory writer. 1M messages: 2.20 GB peak and 40.9 s before, 214 MB and 42.6 s after (Feed A, intent log, book key).
* Generated files go to `data/` (git-ignored) and are never committed.

### Outputs

| File | Content |
|---|---|
| `feedA.pcap`, `feedB.pcap` | Nanosecond-resolution pcap. Ethernet + IPv4 + UDP to the channel's multicast group. IP/UDP headers big-endian, B3 payload little-endian |
| Intent log | One row per message: feed packet sequenceNumber, message index within the packet, templateId, the header's blockLength and version (decided in step 7c, on every row), and every field value as the publisher intended. JSON Lines. An unknown template's row has fields {"body": hex}; the last part of a News also carries "news": {headline, text} with the reassembled text |
| Book key | The book a correct client holds (not the engine's book), top 5 levels per side per instrument, at checkpoints (e.g. every 1,000 events, and after every rebuild). The two differ between a ChannelReset and an instrument's rebuild, where the client holds an empty book. With gaps, books listed as unreliable in the manifest diverge from the key from their first missing packet |

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
| J | Rebuild interleaving and resent statistics | v1 rebuilds every instrument of a reset, then resumes live flow. B3 can interleave live updates of an instrument already rebuilt with other instruments' recovery (LAYOUTS.md 12.9, Guidelines p.46 item 6). B3 may also re-send group phases, statuses, statistics and bands after a ChannelReset or EmptyBook (Guidelines pp.47-48); v1 does not |
| K | Null-price (market-on-auction/close) orders | Out of v1: they only exist in auctions, which v1 does not model. LAYOUTS.md 12.3: they form the best level on either side |
| L | TradeBust and statistics | Inferred: a TradeBust leaves statistics (ExecutionStatistics counts and volume, High/Low, last price) unchanged. B3 does not say whether it republishes them |
| M | News details | Inferred: the headline is in part 1 only; each part is a standalone message with EndOfEvent (0x80); market-wide News (null securityID) goes on one channel (70 in the default config); text is ASCII so byte splits never cut a character. B3: newsID is sent only on the Unified News Channel ("not sent for trading engine News messages", XML), so News on trading channels has a null newsID and a client groups the parts by channel and order; inferred: the parts of one News are never interleaved with another News on the same channel |
| N | ExecutionSummary cxlQty | Inferred: cxlQty ("total quantity canceled during matching process", XML) is the market or IOC remainder discarded when matching stops, 0 otherwise. B3's own example is self-trade prevention, which v1 does not model |
| O | Price bands on tick | Inferred: PriceBand limits are ±band_bps of the previous close snapped inward to the tick (low up, high down), so both are prices an order can have. B3 does not say how its bands are rounded |
| P | Firm mass cancel | B3: one event across all affected orders, even across instruments, only the last DeleteOrder with EndOfEvent (Guidelines p.15). Inferred: it covers both sides of every instrument of one channel (an event cannot span channels), deletes go per instrument in config order and within one in priority order (bids, then offers) |
| Q | Stream timing around sessions | Inferred: the incremental stream is down overnight and over the weekend (no heartbeats), and comes up 2.5 heartbeat intervals before the session, so exactly 3 start-of-day heartbeats go out (Guidelines p.43 describes start-of-day heartbeats, not how many). The weekly SequenceReset and ChannelReset go out at Monday's start, right before the session, not at system start-up on Sunday |
| R | Orders across sessions | Inferred: every resting order survives the night and the weekend (all behave as good-till-cancelled); no day orders expire at session end |
| S | Next session's values | Inferred/invented: the previous close is the day's ClosingPrice; futures' settlement price equals it; the PriceBand is ±band_bps around it, snapped to tick (item O); QuantityBand and OpenInterest repeat the configured values |
| T | OpeningPrice, HighPrice, LowPrice fields | Inferred: OpeningPrice carries netChgPrevDay = opening price − previous close (8 decimals); HighPrice and LowPrice always use mDUpdateAction NEW (B3 allows NEW or DELETE; DELETE is never sent) |
| U | Statistics fields left null | Inferred: PriceBand tradingReferencePrice and priceBandMidpointPriceType, ClosingPrice lastTradeDate, Trade trdSubType and SecurityGroupPhase tradSesOpenTime are null; QuantityBand avgDailyTradedQty is the configured value (0 for futures) |
| V | TradeBust event shape | Inferred: a TradeBust is its own event (EndOfEvent 0x80) with its own transactTime, copying the busted Trade's securityID, session, price, size, tradeID and tradeDate; no other message goes with it (statistics: item L) |
| W | News field choices | Inferred/invented: newsSource OTHER, languageCode "en", origTime = when the generator publishes it, uRLLink empty, ASCII text; newsID null on trading channels (B3, item M) |
| X | Malformed and schema-evolution handling | Project policy, not B3: B3 says nothing about malformed packets. The expected client behaviour in the malformed manifest (keep the complete messages before a fault, discard the rest of the packet, carry on with sequencing; a datagram shorter than the packet header uses no sequence number) is ours. So is mixing schema versions 14, 15, 17 and 18 on one channel and putting an unknown template inside a live event (B3 only says channels may differ during a rollout) |
| Y | Heartbeat timing | Inferred: a heartbeat goes out when a channel has been idle for exactly heartbeat_ns (1 s), then every 1 s; B3 says "more than 1 second". The difference is below anything a client measures |
| Z | sendingTime precision | B3: sendingTime is in nanoseconds "accurate to microseconds" (Message Reference 5.1). The generator writes the packet's close time to the nanosecond, so values are not multiples of 1,000 ns. Accurate to microseconds does not require that, but a real capture may show it |
