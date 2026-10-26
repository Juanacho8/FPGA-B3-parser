# B3 UMDF FPGA Parser: Project Context

Context for anyone (human or AI) working on this project. It records what the project is, what has been decided and why, and what is still open. B3's own wire format and feed behavior live in `spec/LAYOUTS.md`, which this file does not repeat.

---

## 1. Project in one paragraph

An FPGA parser for B3's Binary UMDF market data feed (SBE encoding), benchmarked against a C++ baseline. Real B3 data needs institutional access, so a Python **feed generator** reproduces B3's incremental feed byte for byte from B3's official schema, including its event structure, sequencing and recovery behavior. All three components (generator, C++ parser, RTL parser) are checked against answer keys the generator produces. Portfolio project. The audience includes a two-person quant desk at C6 Bank in Brazil, who are advising on it. Scope is **B3 only**. Other Brazilian venues (A5X, Base Exchange) are deferred until the B3 FPGA work is done.

## 2. Repository

```
B3_Parser/
  README.md
  spec/                 B3's files only (source of truth)
    b3-market-data-messages-2.3.1.xml
    BinaryUMDF-MessageReference-v.2.3.1-enUS.pdf
    BinaryUMDF-MessageSpecificationGuidelines-v.2.3.1-enUS.pdf
    LAYOUTS.md          verified wire format + feed behavior, B3 facts only
  docs/
    PROJECT_CONTEXT.md        this file (our decisions)
    FEED_GENERATOR_CONTEXT.md build brief for the feed generator
  feed_generator/       Python, standard library only (package: b3gen)
  cpp_baseline/         not started
  rtl_parser/           RTL (contains a placeholder rtl/top/fpga_top.sv)
  data/                 generated captures (pcaps are git-ignored)
```

## 3. Source of truth

* B3 Binary UMDF schema `b3-market-data-messages-2.3.1.xml` (schema id 2, version 17). SHA-256 `b055b9a948756ab795f0f6f645a571094cacb85133e091e94440b812e17503c4`.
* Every offset in `spec/LAYOUTS.md` was computed mechanically from the XML, matched field by field against the Message Reference PDF for all 29 templates, and confirmed by decoding the sample packet B3 prints in the Message Reference.
* Where B3's documents disagree, the XML wins. Known document errors are listed in `spec/LAYOUTS.md` section 13.
* A newer Guidelines revision (v2.3.1.2) exists and has not been diffed against v2.3.1 yet.

## 4. Key B3 facts that drive the design

Details and references in `spec/LAYOUTS.md`.

* Encoding is SBE: every field at a fixed byte offset, little-endian, prices as int64 mantissa x 10^-4.
* UDP multicast. One packet = 16-byte header + one or more whole messages, at most 1400 bytes. Messages never span packets.
* Each message = 12-byte header (length, 0xEB50, blockLength, templateId, schemaId 2, version 17) + body.
* **Decoders must use blockLength and version from each message header**, never the template's assumed length. B3 states this explicitly.
* Heartbeat (Sequence) packets carry sequenceNumber 0, which must be ignored for gap detection.
* Order book is market-by-order with price/priority by secondaryOrderID. A modify that loses priority is sent as DeleteOrder + Order NEW with a new ID. Order CHANGE is only for changes that keep priority (size reductions, partial fills). Trade messages never update the book.
* A matching event starts with ExecutionSummary and its last message has EndOfEvent (bit 7 of matchEventIndicator). Events can span packets, and packets can contain several events.
* Feeds A and B carry identical incremental packets. Per B3 circular OC 131/2023, Feed A has a faster architecture than Feed B. Feed C is the disaster-recovery site. Snapshot and instrument definition streams exist only on Feed A.
* Real Binary UMDF channel numbers (from OC 131/2023): 68, 70, 72, 74, 76, 78, 80, 82, 84, 86, 88, 90, 92, 94, 98. The instrument-to-channel mapping and multicast addresses are in B3's "Market Data Channels Definition" document, which is not public.

## 5. Overall build plan

| Stage | Component | Status |
|---|---|---|
| 1 | Feed generator (Python) | Built (steps 0 to 8 and review fixes, branch `feed-generator`) |
| 2 | C++ baseline parser | Not started |
| 3 | RTL v1: parser (bytes to per-order events) | Not started |
| 4 | RTL v2: A/B line arbitration in front of the parser | Not started |
| 5 | RTL v3: price-level book, top N levels per instrument | Not started |
| Software | Full per-order book, snapshot and instrument definition recovery | Later |

Architecture decisions already made for later stages:
* **One generic parser for all instruments and security types.** The wire format is identical, and securityID is just a number.
* **Channel configuration (channel to instruments, filters) lives in a runtime-writable table** loaded by software. It is not compiled into the design. channelNumber is byte 0 of each packet, so the lookup runs in parallel with the rest of the packet streaming in and adds no latency. Multicast subscription is handled at the network/MAC level, outside the parser.
* **A/B arbitration:** take the first copy of each (channel, sequenceVersion, sequenceNumber) from either feed and drop the duplicate.
* **Snapshot, instrument definition and Feed C handling stay in software.** They are not latency critical.
* **Book state while it is wrong** (mid-event, or after a gap) is handled in the C++ and RTL phases. Planned building blocks: a per-instrument mid-event flag cleared by EndOfEvent, and a per-instrument stale flag set by a sequence or rptSeq gap and cleared by recovery.

## 6. Feed generator: purpose

* Primarily a **build and test tool**: it produces traffic to develop and verify the C++ and RTL implementations against.
* Not intended for absolute performance claims. It may be used for relative timing comparisons between implementations.
* Must reproduce what B3 sends on the incremental stream as faithfully as the public documents allow.

## 7. Feed generator: architecture

Five layers, each feeding the next. The **order flow layer and the matching engine are separate components**, so the order flow model can be upgraded, or replaced with real order data, without touching anything else.

| Layer | Role |
|---|---|
| 1. Order flow | Produces order instructions with timestamps: new limit order, cancel, modify, aggressive order |
| 2. Matching engine | One book per instrument, price/priority by secondaryOrderID. Applies instructions and returns outcomes (rested, reduced, requeued, fills) |
| 3. Market data publisher | Turns outcomes into B3 messages following B3's rules. Owns rptSeq, matchEventIndicator, secondaryOrderID/tradeID assignment and statistics state |
| 4. Packetizer | Groups messages into packets (policy in section 8), stamps packet headers, emits heartbeats, produces Feed A and Feed B |
| 5. Wire writer | Ethernet/IPv4/UDP framing and pcap output |

Supporting module: `layouts.py`, the field offset and size definitions used by the encoders, generated from the XML (section 12, item B).

## 8. Feed generator: decisions

### Scope

* **Incremental stream only** for v1. Snapshot recovery and instrument definition streams are v2, needed when the book and recovery logic is built.
* **No opening auction.** Every session starts already in continuous trading.
* **B3 restarts and resets are in scope** (section 9).

### Order flow model

* Simple "zero-intelligence" model: random limit orders near the mid price, random cancels and size changes, occasional aggressive orders that cross the spread.
* **Bursty timing now:** arrivals alternate between calm and busy regimes, e.g. a Poisson process whose rate switches between regimes. Real markets are bursty, and uniform timing would understate parser stress.
* Fancier behavior (realistic queue dynamics, order clustering, replaying real orders) is deferred. The layer separation in section 7 makes it a drop-in change.

### Packetization: option 3

* A packet is closed when **the next message would exceed 1400 bytes, or a time window has elapsed since the packet's first message**. The default window is 5 µs, and it is configurable.
* Window 0 approximates one message per packet. An infinite window approximates "always fill". Both extremes stay available for stress tests.
* B3 does not document its batching rule. Option 3 is our inference and gets tuned if a real capture becomes available.
* The policy is a swappable setting in the packetizer.

### Messages emitted

Following what B3 sends during continuous trading:

| Message | When |
|---|---|
| Order_MBO (50), DeleteOrder_MBO (51) | Every book change |
| MassDeleteOrders_MBO (52) | Scenario |
| ExecutionSummary (55) | First message of every matching event |
| Trade (53) | Each fill |
| HighPrice (24) / LowPrice (25) | When a trade sets a new session high or low |
| OpeningPrice (15) | First trade of the session |
| ExecutionStatistics (56) | Each trade event: volume, VWAP, number of trades, change vs previous close |
| ClosingPrice (17) | Session start (previous close) and session end |
| PriceBand (22) / QuantityBand (21) | Session start |
| SettlementPrice (28) / OpenInterest (29) | Session start, futures only |
| SecurityGroupPhase (10) | Session start (TRADING_SESSION_CHANGE). SecurityStatus (3) is not emitted in v1 |
| Sequence (2) | Heartbeat when idle more than 1 s, in a packet with sequenceNumber 0 |
| SequenceReset (1), EmptyBook (9), ChannelReset (11) | Restart and reset scenarios |
| TradeBust (57) | Scenario |
| News (5) | Scenario. Main test of variable-length data and multi-part messages |

Not emitted for now: LastTradePrice (mostly a snapshot-stream message), TheoreticalOpeningPrice and AuctionImbalance (auctions only), ForwardTrade (forward contracts only), SecurityDefinition on the incremental stream.

**Event message order.** B3's examples do not fix the order inside an event, so the generator uses this documented order:
1. ExecutionSummary
2. Trades
3. Book updates for resting orders
4. Statistics (OpeningPrice, HighPrice, LowPrice)
5. ExecutionStatistics with EndOfEvent

Parsers must not depend on this order.

### Instruments and channels

| Instrument | Type | Tick | Channel |
|---|---|---|---|
| WIN | Mini Ibovespa future | 5 points | 68 |
| DOL | US dollar future | 0.5 | 68 |
| PETR4 | Equity | 0.01 | 70 |
| VALE3 | Equity | 0.01 | 70 |

* Channel numbers are real B3 values. The instrument mapping, securityIDs and multicast addresses/ports are invented.
* All of it lives in **one config file**, swapped for B3's real channel definition when available.

### Feeds

* Feed A and Feed B written as **two separate pcaps**.
* B is a copy of A with a small extra delay. A occasionally drops packets that B still carries, so arbitration has something to test.

### Other defaults

| Item | Default |
|---|---|
| Clock | Simulated nanosecond clock. sendingTime, transactTime and mDInsertTimestamp derived in a consistent order |
| IDs | secondaryOrderID unique across all market segments and incremental per segment (Guidelines p.68): one counter per segment, each in its own range. tradeID per instrument per day, rptSeq per instrument |
| pcap | Nanosecond-resolution pcap |
| Determinism | Seeded random number generator. Same seed and config gives byte-identical output |
| File sizes | Small (~20k messages) for RTL simulation, large (~1M+) for C++ runs, plus a burst-heavy file |

## 9. Scenarios

Kept in separate files from the clean main file.

| Scenario | Content |
|---|---|
| Loss | Packets dropped from Feed A, still present on Feed B |
| Gaps | Packets missing from both feeds, so gaps show in sequenceNumber and rptSeq |
| Idle | Quiet periods with heartbeat packets (sequenceNumber 0) |
| Weekly start | sequenceVersion +1, sequenceNumber back to 1, books rebuilt |
| Engine failover | SequenceReset, sequenceVersion +1, then per instrument EmptyBook + Order messages with RecoveryMsg (bit 5) |
| ChannelReset | ChannelReset, then the same per-instrument rebuild |
| Single-instrument reset | EmptyBook + recovery orders for one instrument, whose rptSeq restarts at 1 |
| New trading session | SecurityGroupPhase with TRADING_SESSION_CHANGE, statistics reset |
| Edge orders | Market supervision DELETE_THRU, a firm's mass cancel across instruments, aggression sweeping several levels, events spanning packets (null-price orders are out of v1: they only exist in auctions) |
| Schema evolution | A message with a larger blockLength than the schema, and an unknown templateId. Tests "trust the header" |
| Rare messages | TradeBust, multi-part News |
| Malformed | Truncated packets, bad lengths. Separate small file |

Recovery is faithful because the generator's engine always knows the true book. A rebuild republishes that book, and the answer keys show the correct client state afterwards.

## 10. Feed generator: outputs

* `feedA.pcap`, `feedB.pcap`
* **Intent log:** one row per message published, with every field value as the publisher intended. Built from the publisher's own data, **never by decoding the generated bytes**. Otherwise an encoder bug could hide itself.
* **Book key:** true top-of-book (top 5 levels per instrument) at checkpoints, e.g. every 1,000 events. Used later to test book building.
* The parser output format (section 12, decision A) does not exist yet. When it does, a small script converts the intent log into that format.

## 11. Feed generator: build order

| # | Step | Done when |
|---|---|---|
| 0 | Skeleton inside `feed_generator/` | pytest runs |
| 1 | `layouts.py` | Matches `spec/LAYOUTS.md` for every template the generator emits |
| 2 | Encoders | B3's sample packet (Message Reference section 6) re-encodes byte for byte |
| 3 | Packetizer (option 3) + pcap writer | Opens in Wireshark. Packets at most 1400 B. Sequencing and heartbeat rules hold |
| 4 | Matching engine (no market data) | Unit tests: priority ordering, partial fills, sweeps, cancels, requeue on size increase |
| 5 | Market data publisher | Each engine outcome produces exactly B3's message pattern. EndOfEvent only on the last message. Statistics correct |
| 6 | Order flow with bursty timing + intent log + book key | Same seed gives identical output |
| 7 | Feed B and scenarios | Each scenario appears where expected |
| 8 | CLI and config file | Small, large and burst files generated from one command |

## 12. Open items

| # | Item | Status |
|---|---|---|
| A | **Parser output contract**: what a decoded message looks like when handed to the next stage (fixed-width record vs per-type records, which fields). Leaning fixed-width for FPGA friendliness | Deferred to the C++/RTL phase. Will live in `docs/DESIGN.md` |
| B | `layouts.py`: generated from the XML, or hand-written with a test checking it against the XML | **Resolved.** One repo-level script, `tools/gen_layouts.py`, generates both `feed_generator/b3gen/layouts.py` and `cpp_baseline/include/b3/layouts.hpp` from the XML in one run. It refuses to run unless the XML's SHA-256 matches `spec/LAYOUTS.md`. Outputs checked in; a staleness check (`make layouts-check`, run by `make cpp-test`, plus the generator's tests) fails if either differs from a fresh run. See `docs/FEED_GENERATOR_CONTEXT.md` section 12 |
| C | Diff Guidelines v2.3.1.2 against v2.3.1 | Open |

## 13. Things to ask the C6 quants for

* A short real Binary UMDF capture (pcap), even a few minutes. It is the only way to validate against reality and to tune the packetization window.
* B3's Market Data Channels Definition (channel to instruments, multicast addresses).
* Access to B3's certification environment, if possible.
* Later: A5X specifications, if C6 has portal access.
