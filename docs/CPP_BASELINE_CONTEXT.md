# C++ Baseline: Build Context

Brief for the chat or agent building the C++ baseline. Read this first, then:

* `spec/LAYOUTS.md`: B3's wire format and feed rules. Every byte the decoder reads is defined there.
* `docs/PROJECT_CONTEXT.md`: whole-project context.
* `docs/FEED_GENERATOR_CONTEXT.md` and `feed_generator/README.md`: what the test data contains and how its answer keys are laid out (`intent.jsonl`, `book_key.jsonl`, `manifest.json`). Its open items (section 12) list every place the generator made an inference B3 does not document.
* `spec/b3-market-data-messages-2.3.1.xml`: B3's schema. Source of truth when anything disagrees.

---

## 1. What we are building and why

A C++ feed handler for B3 Binary UMDF: pcap in, two record streams out. It has three jobs:

1. **Correctness reference.** It is the first real decoder of the generator's pcaps, checked against the generator's answer keys.
2. **The RTL's golden model.** The FPGA must produce the same two output files bit for bit. Anything the C++ decides (record layout, book algorithm, gap handling, malformed-packet policy) is what the RTL copies.
3. **Software baseline for timing.** Same input as the FPGA, measured from packet in to event out and packet in to book update out. Relative comparisons only, per the project's rules.

## 2. Working rules

* **Only touch** `cpp_baseline/`, repo-level `tools/`, `docs/DESIGN.md`, and add targets to the root `Makefile`. Moving `gen_layouts.py` (step 2) is the one allowed change in `feed_generator/`, and the generator's tests must stay green.
* **Build one step at a time** (section 9). Stop at each step's "done when" check.
* **Answer keys come from the generator**, never from the C++ itself. A C++ output is only "right" when it matches a key produced independently.
* **The C++ never parses JSON.** It writes JSON Lines (full decode) and binary records. Comparisons against the generator's JSON keys are done by Python scripts in `tools/`.
* Gabriel builds with Claude Code and wants to understand each piece well enough to explain it. Clear code over clever code, with a short comment block at the top of each file saying what it does and what it must not do.

## 3. Architecture

```
pcap reader -> packet decoder -> message walker -> message decoders -> event sink ───────> port 1: events.bin
               (sequencing)      (framing)                   │
                                                             ├─> price-level book -> book sink ──> port 2: book.bin
                                                             └─> full decode writer ───────────> decode.jsonl (debug)

events (port 1) ──> MBO book (behind a flag) ──> cross-check against the price-level book
```

A/B arbitration slots in front of the packet decoder in step 11. Every stage has an RTL counterpart, so keep stage boundaries clean.

| Stage | Job | Must not |
|---|---|---|
| Pcap reader | Load the whole file into memory. Strip Ethernet, IPv4, UDP. Yield UDP payload, capture timestamp, and which multicast group it came from | Interpret B3 bytes |
| Packet decoder | 16-byte packet header. Per-channel sequencing (section 6) | Look inside messages |
| Message walker | Step through messages by messageLength. Check encodingType 0xEB50 and schemaId 2. Dispatch on templateId. Apply the malformed policy (section 6) | Assume a template's length |
| Message decoders | Read fields at fixed offsets from the generated header, honoring the header's blockLength and version | Allocate, or use virtual calls |
| Event sink (port 1) | One 64-byte record per hot-path message (section 4) | |
| Price-level book | Depth-limited book, the algorithm the RTL will implement (section 5) | Keep per-order state |
| Book sink (port 2) | One book update per completed event (section 4) | |
| Full decode writer | Every field of every message as JSON Lines, in the same shape as `intent.jsonl` | Run inside timed code |
| MBO book (flag) | Per-order book built from port 1 records only. Its price-level totals must equal the price-level book after every event | |
| Benchmark harness | Timestamps around the hot path (section 8) | |

### Hot-path coding rules

* Whole pcap preloaded. No file I/O, printing or allocation inside the processing loop. Output records go to preallocated buffers, written to disk afterwards.
* Zero-copy: decoders read straight from the packet buffer.
* Unaligned little-endian loads through one small helper (memcpy-based). Never cast a byte pointer to a struct or integer pointer.
* No virtual functions in the hot path. Dispatch by `switch` on templateId. Sinks are template parameters so the compiler can inline them.
* Fixed-size structs with `static_assert` on their sizes and offsets.

## 4. The two output contracts (finalize in step 1, in `docs/DESIGN.md`)

Both are shared with the RTL, little-endian, packed, fixed size. They are **venue-neutral** (the root README describes a venue-neutral normalized stream): the main type field is our own event kind, and B3's templateId is kept as a detail. A5X can later feed the same records.

### Port 1: event record, 64 bytes (proposal)

| Off | Size | Field | Notes |
|---|---|---|---|
| 0 | 4 | seq_num | Packet sequenceNumber |
| 4 | 2 | seq_version | Packet sequenceVersion |
| 6 | 1 | channel | |
| 7 | 1 | msg_index | Index of the message within its packet |
| 8 | 1 | kind | Normalized kind (below) |
| 9 | 1 | flags | Bit 0 EndOfEvent, bit 1 RecoveryMsg, bit 2 Implied, others reserved |
| 10 | 1 | side | 0 none, 1 bid, 2 offer |
| 11 | 1 | venue_msg_type | B3 templateId (all B3 ids fit in a byte) |
| 12 | 4 | rpt_seq | 0 when the message has none |
| 16 | 8 | instrument_id | B3 securityID |
| 24 | 8 | price | int64 mantissa x 10^-4. Null keeps B3's null (INT64_MIN) |
| 32 | 8 | qty | |
| 40 | 8 | order_id | secondaryOrderID, or tradeID for trades |
| 48 | 8 | exch_time | transactTime (or mDEntryTimestamp where that is the message's time field) |
| 56 | 8 | aux | Meaning by kind: ADD = enteringFirm, MODIFY = previous qty, TRADE = buyer << 32 \| seller, others 0 |

Proposed kinds and their B3 sources (hot path only):

| Kind | B3 source |
|---|---|
| ADD | Order_MBO NEW (50) |
| MODIFY | Order_MBO CHANGE (50) |
| DELETE | DeleteOrder_MBO (51) |
| CLEAR_SIDE | MassDeleteOrders_MBO DELETE_THRU (52) |
| CLEAR_BOOK | EmptyBook (9) |
| CLEAR_CHANNEL | ChannelReset (11) |
| EXEC_SUMMARY | ExecutionSummary (55): price = lastPx, qty = fillQty, aux = aggressorSide |
| TRADE | Trade (53) |
| TRADE_BUST | TradeBust (57) |
| SESSION | SecurityGroupPhase (10), SecurityStatus (3) |
| SEQ_RESET | SequenceReset (1) |

Not on port 1: statistics (15, 17, 21, 22, 24, 25, 28, 29, 56), News (5), heartbeats (2), unknown templates. They still appear in the full decode, and they still close events: **a message not on port 1 that carries EndOfEvent must still close the event for the book** (ExecutionStatistics usually ends a trade event). Decide in step 1 whether port 1 carries an END_OF_EVENT marker record for that case, or the EndOfEvent flag is moved onto the last port 1 record of the event. The RTL needs the same rule.

### Port 2: book update (proposal)

* Identity of the message that completed the event: seq_num, seq_version, channel, msg_index.
* instrument_id, flags (stale, recovery, depth incomplete per side), number of valid levels per side.
* Top N levels per side (N = published depth, 5 by default), best first, each `price (int64), qty (int64), order_count (uint32)`.

The record size depends on N, so every `book.bin` starts with a small file header (magic, format version, N, K, instrument capacity). Tools and cocotb testbenches read the layout from it instead of assuming N = 5.

Emitted when an event that changed an instrument's book completes, only for instruments not stale. A completed event that touched several instruments (a firm mass cancel across instruments) emits one update per instrument touched.

## 5. Price-level book (same algorithm as the RTL)

B3's messages carry enough to maintain price levels without storing orders:

| Message | Update |
|---|---|
| Order_MBO NEW | level[side][price]: qty += size, count += 1 |
| Order_MBO CHANGE | qty += size − mDEntryPrevSize |
| DeleteOrder_MBO | qty −= size, count −= 1 (B3 sends the order's price and remaining size) |
| MassDeleteOrders_MBO | Clear one side |
| EmptyBook | Clear both sides of one instrument |
| ChannelReset | Clear every book on the channel |

Trades never touch the book (B3 rule).

### Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `PUBLISHED_DEPTH` (N) | 5 | Levels per side in each book update |
| `TRACKED_DEPTH` (K) | 16 | Levels per side kept internally. **Will be swept: K = 32 is the next target**, depending on whether the RTL can compare 32 slots within one clock cycle |
| `MAX_INSTRUMENTS` | 64 | Books held. 4 used by the test data |

Rules:
* All three live in **one place**, a single `book_params.hpp` whose values can be overridden from CMake (`-DB3_TRACKED_DEPTH=32`). Nothing else in the code may hardcode 5, 16 or 64. Ideally the same parameter file later generates the RTL's SystemVerilog package, so C++ and hardware cannot drift.
* The book is a template on these values, so changing K is a rebuild with one flag, no code edits. Root `Makefile` targets build side-by-side variants (for example `build-k16/`, `build-k32/`).
* Constraint checked at compile time: N ≤ K.
* Output files record the parameters (port 2 file header, counters JSON), so a golden file is never compared against a build with different values.
* **K changes the results**, not just performance: a smaller K runs out of depth more often. So RTL golden files are generated per (N, K) pair. The RTL must be compared against a C++ build with the same parameters.
* The RTL may pipeline the comparison over 1 or 2 clock cycles to meet timing at K = 32. That changes latency, not results, so the C++ golden stays valid.

### Algorithm: depth-limited sorted levels

Per instrument and side: up to K slots `(price, qty, order_count)` sorted best first, a count of valid slots, and a `truncated` flag.

**Invariant:** the slots hold every level at or better than a boundary price B. If `truncated` is false, the slots hold the whole side. If true, levels worse than B may exist but are unknown.

| Message | Action |
|---|---|
| Order_MBO NEW at price p | Level exists: qty += size, count += 1. Otherwise, if not truncated or p is at or better than B: insert in sorted position. If that makes K + 1 levels, drop the worst, set `truncated`, and B becomes the new worst slot. If truncated and p is worse than B: ignore (untracked region) |
| Order_MBO CHANGE | Level in slots: qty += size − mDEntryPrevSize. Worse than B while truncated: ignore |
| DeleteOrder_MBO | Level in slots: qty −= size, count −= 1, remove the slot when count reaches 0 (B unchanged). Worse than B while truncated: ignore |
| MassDeleteOrders_MBO, EmptyBook, ChannelReset | Clear the side(s): no slots, `truncated` false |
| Any update to a price at or better than B that should be in the slots but is not | Consistency error counter (should never happen) |

**Published levels:** the first min(N, valid) slots. These are always correct.

**Depth incomplete:** a side is incomplete when it is truncated and has fewer than N valid slots, because unknown levels beyond B might belong in the published N. The flag goes out on port 2. It clears when the side is cleared, or when valid slots reach N again. A host refill path (software pushing levels back from its MBO book) is out of scope for v1.

This is a deliberately simple, exact rule. Both C++ and RTL implement it slot for slot.

### Measuring K

* The MBO book (step 9) knows the true top N at all times. The depth-limited book must equal it whenever the side is not depth incomplete. Every mismatch outside that case is a bug.
* Report per run: how often each side becomes depth incomplete, how long it stays that way, and the maximum number of levels seen per side. Run with K = 16 and K = 32. That is the evidence for choosing K.
* **The generator's current books are thin** (orders a few ticks from mid, `thin_levels = 3`), so K = 16 may never truncate on them. Unit tests use a tiny K (for example 4) to exercise the truncation paths. A deep-book data set (more levels, larger sweeps) is a request for the feed generator, listed in open items.

**Instruments:** a runtime table maps securityID to a book slot (up to `MAX_INSTRUMENTS`), loaded from a config file. Messages for instruments not in the table are still decoded and appear on port 1, but are not booked (counter).

**Null-price (market-on-auction) orders** do not occur in v1 data (generator open item K). Count them separately, outside the slots. B3 says they form the best level, so a later version can expose them.

**Flags per instrument:**
* mid_event: set by a book change, cleared when the event's EndOfEvent message arrives.
* stale: set when the instrument's rptSeq jumps (a message for it was lost), or when a lost packet held a SecurityGroupPhase, ChannelReset or SequenceReset (then the whole channel). Cleared by a rebuild (EmptyBook or ChannelReset followed by recovery orders). Must reproduce `manifest.json` `unreliable_books`.

**Validation:** at every `book_key.jsonl` checkpoint, an instrument's top 5 (the key's depth, so this check assumes N = 5) must equal the key, unless that side is depth incomplete, when its channel reaches `as_of`. The key shows the client's book, so a book emptied by a ChannelReset and not yet rebuilt is empty in the key too.

## 6. Sequencing and malformed packets

Per channel (rules in `spec/LAYOUTS.md` 12.1, matching `feed_generator/tests/book_builder.py`):
* sequenceNumber +1 per packet within a sequenceVersion. Higher than expected: gap of the difference. Lower or equal: duplicate.
* Heartbeats (sequenceNumber 0): not sequenced. Their nextSeqNo must equal the next expected number.
* A packet holding SequenceReset is accepted without checking its own sequenceNumber or version. The next packet must be sequenceVersion +1 (1 after 65534), sequenceNumber 1.
* rptSeq per instrument: previous + 1, or 1 on the message after an EmptyBook.
* Counters: packets, messages per template, gaps (packets), duplicates, rptSeq gaps per instrument, resets, malformed, unknown templates. Written to a counters JSON at the end of a run and compared with `manifest.json`.

**Malformed policy** (project policy from the generator's `malformed` manifest, not B3): process the complete messages before the fault, discard the rest of the packet, carry on with sequencing. A datagram shorter than the 16-byte packet header is discarded and uses up no sequence number.

**Schema evolution** (`schema` scenario): decode known fields using the header's blockLength and version. A field exists only if it lies within blockLength and its sinceVersion is at most the header's version (the scenario mixes versions 14, 15, 17 and 18). Skip extra root bytes. Skip unknown templates by messageLength. The book still matches the key.

## 7. Field offsets

* Move `feed_generator/tools/gen_layouts.py` to a repo-level `tools/gen_layouts.py` that emits both `feed_generator/b3gen/layouts.py` (unchanged output) and `cpp_baseline/include/b3/layouts.hpp`. Later it also emits a SystemVerilog package for the RTL.
* The C++ header holds per-template constants: blockLength, field offsets, sizes, sinceVersion, null values, enum values. Checked in, never edited by hand.
* A C++ test checks a few offsets against `spec/LAYOUTS.md` values, and the generator's existing test keeps guarding the Python output.

## 8. Benchmark rules

* Release build (`-O3`, native CPU). Separate Debug build with AddressSanitizer and UndefinedBehaviorSanitizer for tests.
* Pcap preloaded. Output to preallocated buffers. No I/O in the timed loop.
* Warm-up pass, then many runs. Report the spread, not one number.
* Two measurements: packet start to its last event record written, and packet start to book update written. Plus throughput (messages and packets per second).
* **Gabriel's machine is an Apple Silicon Mac.** No core pinning, no x86 timestamp counter. `std::chrono::steady_clock` ticks at about 42 ns there, which is coarser than one message's decode time. Time per packet or per batch of packets and divide, and report the clock resolution alongside results. Numbers are relative, with the hardware stated.
* Data: `large/` (1M messages) for throughput, `burst/` for tail latency.

## 9. Build steps

| # | Step | Done when |
|---|---|---|
| 0 | `cpp_baseline/`: CMake (C++20), doctest single header in `third_party/`, Release and Debug+sanitizer builds, root `Makefile` targets | Builds, an empty test passes |
| 1 | `docs/DESIGN.md`: both contracts and the event-closing rule. `tools/` scripts converting `intent.jsonl` into expected `events.bin`, and comparing a C++ `decode.jsonl` with `intent.jsonl` | Contracts agreed. Converter output matches hand-checked examples |
| 2 | Move `gen_layouts.py` to `tools/`, emit `layouts.hpp` | Generator tests green. C++ offset test passes |
| 3 | Pcap reader | Packet and byte counts per feed match the generator's files |
| 4 | Packet decoder + message walker | Message count per template matches `intent.jsonl`. `malformed` and `schema` files handled per section 6 |
| 5 | Message decoders + full decode | `decode.jsonl` matches `intent.jsonl` field for field on every set, including `null` and `absent`. B3's sample packet (`feed_generator/tests/fixtures/b3_sample_packet/`) decodes to `decoded.json` |
| 6 | Sequencing and counters | Each scenario's counters match its `manifest.json` (gaps, duplicates, resets, unreliable books) |
| 7 | Event sink (port 1) | `events.bin` byte-identical to the converted intent log on every set. **First RTL golden** |
| 8 | Depth-limited book + book sink (port 2), parameters in `book_params.hpp` | Unit tests pass for K = 4, 16 and 32 (truncation paths covered). Matches `book_key.jsonl` at every checkpoint on every set and scenario, stale flags match `unreliable_books`. **Second RTL golden, one per (N, K)** |
| 9 | MBO book behind a flag | Its top N equals the depth-limited book after every event whenever the side is not depth incomplete. Delete and CHANGE consistency checks (stored price, size, prevSize) all pass. Depth-incomplete statistics reported for K = 16 and K = 32 |
| 10 | Benchmark harness | Reproducible percentiles for both measurements on `large/` and `burst/` |
| 11 | A/B arbitration | `loss` scenario: merged A + B gives the same outputs as `clean`-style complete data and matches the book key everywhere. `gap`: matches except `unreliable_books` |

Steps 7 and 8 unblock the RTL.

**Independent book golden for port 2.** The book key only has checkpoints. For an every-event check, a Python script in `tools/` can replay the pcap through the generator's independent test decoder and book builder (`feed_generator/tests/sbe_decoder.py`, `book_builder.py`, which never import b3gen) and write the expected `book.bin`. Worth adding in step 8.

## 10. Suggested layout

```
cpp_baseline/
  CMakeLists.txt
  README.md
  include/b3/
    layouts.hpp        generated
    wire.hpp           unaligned little-endian loads
    pcap.hpp           pcap reader
    packet.hpp         packet header, sequencing, counters
    walker.hpp         message framing, malformed policy
    decode.hpp         per-template field access
    records.hpp        port 1 and port 2 structs, static_asserts
    book_params.hpp    N, K, MAX_INSTRUMENTS (the only place they are set)
    book_level.hpp     depth-limited price-level book, templated on the parameters
    book_mbo.hpp       per-order book (flag)
    arbiter.hpp        A/B arbitration (step 11)
  src/                 implementations
  apps/
    b3_decode.cpp      pcap -> events.bin, book.bin, decode.jsonl, counters.json
    b3_bench.cpp       benchmark harness
  tests/               doctest unit tests
  third_party/doctest.h
tools/                 repo level: gen_layouts.py, contract converters, comparison scripts
```

## 11. Decisions taken

| Item | Decision |
|---|---|
| Book in the FPGA | Price-level book now, not deferred, kept depth-limited to fit small FPGAs. The C++ implements the identical algorithm |
| Book parameters | Published depth N = 5, tracked depth K = 16, 64 instruments. All three build parameters set in one place. K = 32 is the next target, pending RTL timing |
| Outputs | Two independent ports: event records per message, book updates per completed event. Separate files |
| MBO book | Software only, built from port 1. In C++ behind a flag, cross-checking the price-level book. MBO in RTL is a later stretch option |
| Event scope | Hot path only on port 1. Statistics and News in the full decode |
| Records | Fixed width, venue-neutral kinds, B3 templateId as a detail |
| Book storage | Depth-limited sorted levels (K slots per side) with a truncated flag and a depth-incomplete flag. Replaces the earlier price-array idea, which needed about 100 KB per futures instrument |
| Publish rule | At EndOfEvent, only when not stale |
| Offsets | Generated from B3's XML by a shared repo-level script |
| A/B arbitration | Last core step. Take the first copy of each (channel, sequenceVersion, sequenceNumber) |

## 12. Open items

| # | Item | Status |
|---|---|---|
| A | Final field lists of both records | Proposals in section 4, finalize in step 1 |
| B | How an event closed by a non-port-1 message is signalled on port 1 | Decide in step 1 |
| C | K = 16 or 32 | Decided by RTL timing plus the depth-incomplete statistics from step 9 |
| D | A/B arbitration wait window for a missing packet | Decide in step 11 |
| E | Whether port 2 also emits on mid-event changes (with a flag) | Default no |
| F | Deep-book data set from the feed generator (more levels per side, sweeps through several levels) so K actually gets exercised | Request for the generator, outside this build's folders |
| G | Host refill of a depth-incomplete side from the software MBO book | Out of scope for v1 |
| H | One parameter file generating both `book_params.hpp` and the RTL's SystemVerilog package | Desirable, decide when the RTL book starts |
