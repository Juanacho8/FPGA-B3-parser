# Output contracts: C++ baseline and RTL

The two output streams that the C++ baseline and the FPGA both produce, bit for bit. Port 1 carries one event record per hot-path message. Port 2 carries one book update per completed event and instrument. This file is the specification. `tools/b3contract.py` and `tools/intent_to_events.py` are one independent reading of it, and the hex examples in section 3.9 are checked against them by `tools/tests/`.

B3's wire format is in `spec/LAYOUTS.md`, the build brief in `docs/CPP_BASELINE_CONTEXT.md`.

Format version: **1**.

---

## 1. Conventions

* Every multi-byte field is **little-endian**. Records are packed, with no implicit padding. Every field sits at its natural alignment inside the record (a field of size s starts at an offset that is a multiple of s).
* Types: `u8`, `u16`, `u32`, `u64` unsigned, `i64` signed two's complement.
* Widths are B3's wire widths: prices and quantities `i64`, order counts `u32`. Values are copied unchanged from the wire, with no scaling. Prices are mantissas ×10⁻⁴.
* **Null price**: a price whose wire value is B3's null (INT64_MIN, `0x8000000000000000`) is written as INT64_MIN.
* **Absent fields**: a field that the message's schema version does not have (its `sinceVersion` is above the header's version, or it lies beyond blockLength) is written as that field's null value. For the fields port 1 reads, that is `transactTime` → 0, `mDEntryPx` → INT64_MIN, `mDEntryPrevSize` → INT64_MIN.
* **Unused fields**: a field a record kind does not use is **zero**. So is every reserved byte and reserved bit.
* "Channel" is the packet header's channelNumber.

## 2. File header (both files)

`events.bin` (port 1) and `book.bin` (port 2) each start with one 32-byte header, followed by the records back to back. Readers take the layout from the header and never assume it.

| Off | Size | Type | Field | Value |
|---|---|---|---|---|
| 0 | 8 | char[8] | magic | `"B3PORT1\0"` (events.bin) or `"B3PORT2\0"` (book.bin) |
| 8 | 2 | u16 | format_version | 1 |
| 10 | 2 | u16 | header_size | 32. Records start at this offset |
| 12 | 4 | u32 | record_size | 64 for events.bin, `book_record_size(N)` for book.bin (section 4.1) |
| 16 | 2 | u16 | published_depth | N of the build |
| 18 | 2 | u16 | tracked_depth | K of the build, **0 = uncapped** book |
| 20 | 4 | u32 | max_instruments | MAX_INSTRUMENTS of the build |
| 24 | 8 | u64 | reserved | 0 |

* The record count is not stored. It is `(file size − header_size) / record_size`, and a file whose remainder is not zero is corrupt.
* events.bin records the book parameters too, although port 1 does not depend on them. That way a golden file always names the build it came from. A comparison treats a header difference as a mismatch.
* A reader rejects an unknown magic or format_version.

Default header (N = 5, K = 16, 64 instruments), events.bin:

<!-- example:events_header -->
```text
42 33 50 4f 52 54 31 00 01 00 20 00 40 00 00 00
05 00 10 00 40 00 00 00 00 00 00 00 00 00 00 00
```

---

## 3. Port 1: event records

### 3.1 Record layout (64 bytes)

| Off | Size | Type | Field | Meaning |
|---|---|---|---|---|
| 0 | 4 | u32 | seq_num | Packet header sequenceNumber |
| 4 | 2 | u16 | seq_version | Packet header sequenceVersion |
| 6 | 1 | u8 | channel | Packet header channelNumber |
| 7 | 1 | u8 | msg_index | 0-based position of the message in its packet, counting every message on the wire (including those not on port 1) |
| 8 | 1 | u8 | kind | Normalized kind (3.2) |
| 9 | 1 | u8 | flags | 3.3 |
| 10 | 1 | u8 | side | 0 none, 1 bid, 2 offer |
| 11 | 1 | u8 | venue_msg_type | B3 templateId of the message (every B3 id fits in a byte) |
| 12 | 4 | u32 | rpt_seq | rptSeq, 0 when the kind does not use it (B3's null is also 0) |
| 16 | 8 | u64 | instrument_id | securityID |
| 24 | 8 | i64 | price | |
| 32 | 8 | i64 | qty | |
| 40 | 8 | u64 | order_id | secondaryOrderID, or tradeID (zero-extended) |
| 48 | 8 | u64 | exch_time | The message's exchange time (3.4), ns since epoch |
| 56 | 8 | u64 | aux | Per kind (3.4). Signed values stored as two's complement |

(seq_num, seq_version, channel, msg_index) is the record's **identity**. It names the message the record came from.

### 3.2 Kinds

| Value | Kind | B3 source |
|---|---|---|
| 0 | (never written) | |
| 1 | ADD | Order_MBO (50), mDUpdateAction NEW (0) |
| 2 | MODIFY | Order_MBO (50), mDUpdateAction CHANGE (1) |
| 3 | DELETE | DeleteOrder_MBO (51) |
| 4 | CLEAR_SIDE | MassDeleteOrders_MBO (52), mDUpdateAction DELETE_THRU (3) |
| 5 | CLEAR_BOOK | EmptyBook (9) |
| 6 | CLEAR_CHANNEL | ChannelReset (11) |
| 7 | EXEC_SUMMARY | ExecutionSummary (55) |
| 8 | TRADE | Trade (53) |
| 9 | TRADE_BUST | TradeBust (57) |
| 10 | SESSION | SecurityGroupPhase (10), SecurityStatus (3) |
| 11 | SEQ_RESET | SequenceReset (1) |
| 12 | END_OF_EVENT | Marker: an event closed by a message not on port 1 (3.5) |
| 13–255 | reserved | |

**Not on port 1**: every other template, including statistics (15, 16, 17, 19, 21, 22, 24, 25, 27, 28, 29, 56), News (5), ForwardTrade (54), SecurityDefinition (12), heartbeats (Sequence, 2) and unknown templates. Also not on port 1: Order_MBO with an mDUpdateAction other than NEW or CHANGE, and MassDeleteOrders_MBO with an mDUpdateAction other than DELETE_THRU. The schema's enum allows other values, but `spec/LAYOUTS.md` 12.3 describes only these for these templates. All of these still appear in the full decode and still take part in event closing (3.5).

### 3.3 Flags and side

| Bit | Flag | Source |
|---|---|---|
| 0 | END_OF_EVENT | matchEventIndicator bit 7. Always 1 on an END_OF_EVENT marker |
| 1 | RECOVERY | matchEventIndicator bit 5 (RecoveryMsg) |
| 2 | IMPLIED | matchEventIndicator bit 4 |
| 3–7 | reserved, 0 | |

A message without a matchEventIndicator (ExecutionSummary, SequenceReset) has flags 0. An END_OF_EVENT marker takes bits 1 and 2 from the closing message's matchEventIndicator.

side, for kinds that use it, comes from mDEntryType: `'0'` (BID) → 1, `'1'` (OFFER) → 2, anything else → 0.

### 3.4 Fields by kind

Every record carries its identity, kind, flags and venue_msg_type. The table gives the rest. A dash means the field is 0.

| Kind | side | rpt_seq | instrument_id | price | qty | order_id | exch_time | aux |
|---|---|---|---|---|---|---|---|---|
| ADD | mDEntryType | rptSeq | securityID | mDEntryPx | mDEntrySize | secondaryOrderID | transactTime | enteringFirm (u32, zero-extended; null 0) |
| MODIFY | mDEntryType | rptSeq | securityID | mDEntryPx | mDEntrySize (new size) | secondaryOrderID | transactTime | mDEntryPrevSize (i64; null or absent INT64_MIN) |
| DELETE | mDEntryType | rptSeq | securityID | mDEntryPx (absent before v15: INT64_MIN) | mDEntrySize (remaining size) | secondaryOrderID | transactTime | – |
| CLEAR_SIDE | mDEntryType | rptSeq | securityID | – | – | – | transactTime | – |
| CLEAR_BOOK | – | – | securityID | – | – | – | mDEntryTimestamp | – |
| CLEAR_CHANNEL | – | – | – | – | – | – | mDEntryTimestamp | – |
| EXEC_SUMMARY | – | rptSeq | securityID | lastPx | fillQty | – | transactTime | aggressorSide (0 none, 1 buy, 2 sell) |
| TRADE | – | rptSeq | securityID | mDEntryPx | mDEntrySize | tradeID | transactTime | `mDEntryBuyer << 32 \| mDEntrySeller` (each u32, null 0) |
| TRADE_BUST | – | rptSeq | securityID | mDEntryPx | mDEntrySize | tradeID | transactTime | – |
| SESSION from 10 | – | – | – | – | – | – | transactTime | session word, below |
| SESSION from 3 | – | rptSeq | securityID | – | – | – | transactTime | session word, below |
| SEQ_RESET | – | – | – | – | – | – | – | – |
| END_OF_EVENT | – | – | – | – | – | – | closing message's time field (table below) | – |

**Session word** (aux of SESSION), byte by byte from the least significant:

| aux byte | SecurityGroupPhase (10) | SecurityStatus (3) |
|---|---|---|
| 0 | tradingSessionID | tradingSessionID |
| 1 | tradingSessionSubID | securityTradingStatus |
| 2 | securityTradingEvent (null 255 kept) | securityTradingEvent (null 255 kept) |
| 3–4 | tradeDate (u16, little-endian) | tradeDate (u16, little-endian) |
| 5–7 | securityGroup, 3 ASCII bytes in wire order | 0 |

**Time field by template**, used for exch_time of port 1 kinds and of END_OF_EVENT markers:

| Templates | Field |
|---|---|
| 3, 10, 50, 51, 52, 53, 54, 55, 57 | transactTime |
| 9, 11, 15, 16, 17, 19, 21, 22, 24, 25, 27, 28, 29, 56 | mDEntryTimestamp |
| 5 | origTime |
| 1, 2, 12, unknown | none (0) |

### 3.5 Event closing

An event is open on a channel from its first message until the message with EndOfEvent. A consumer of port 1 alone (the RTL book, the MBO book) must see where each event ends, including events closed by a message that is not on port 1 (usually ExecutionStatistics). The rule:

* If the message carrying EndOfEvent is on port 1, its record has flag bit 0 set. That holds whether or not an event was open: a standalone message (an Order_MBO NEW that is its own event, a SecurityGroupPhase) also carries bit 0.
* If it is not on port 1, the decoder writes an **END_OF_EVENT marker** in its place, but only when an event is open on that channel. The marker carries the closing message's identity, venue_msg_type = its templateId, exch_time = its time field, flags bit 0 plus its RecoveryMsg and Implied bits. Everything else is 0.
* A message that carries EndOfEvent with no event open (statistics, bands, phases and News, which B3 sends each with EndOfEvent on its own) produces no marker.
* EndOfEvent closes the event for **every instrument touched since the last close on that channel**: one close per channel, not per instrument (a firm's mass cancel is one event across instruments).

**State machine.** One instance per channel, two states. It runs on every message the decoder accepts (3.6), in decoder order, whether or not the message is on port 1.

```
state[ch]   : IDLE | OPEN          (reset value IDLE)
touched[ch] : set of instruments   (reset value empty; used by port 2)

for each accepted message m on channel ch:
    rec  = port-1 record of m, or none                      (3.2)
    mei  = m.matchEventIndicator, or none if the template has none

    if rec: emit rec                                        (with flag bit 0 = mei.bit7)
    if rec changes a book: touched[ch] += rec.instrument   (ADD, MODIFY, DELETE,
                                                            CLEAR_SIDE, CLEAR_BOOK, CLEAR_CHANNEL)
    if mei is none:
        if m is ExecutionSummary: state[ch] = OPEN
        else: no change                                     (SequenceReset, heartbeat,
                                                             SecurityDefinition, unknown template)
    elif mei.bit7 (EndOfEvent):
        if not rec and state[ch] == OPEN: emit END_OF_EVENT marker for m
        close(ch)                                           (port 2: one update per touched
                                                             instrument, section 4.3)
        state[ch] = IDLE; touched[ch] = empty
    else:
        state[ch] = OPEN
```

| State | Message | Port 1 output | Next state |
|---|---|---|---|
| IDLE | on port 1, EndOfEvent | the record, bit 0 set | IDLE |
| IDLE | not on port 1, EndOfEvent | nothing | IDLE |
| IDLE | has matchEventIndicator, no EndOfEvent | the record, if on port 1 | OPEN |
| IDLE | ExecutionSummary | the record | OPEN |
| IDLE or OPEN | no matchEventIndicator (other than 55) | the record, if on port 1 (SEQ_RESET) | unchanged |
| OPEN | on port 1, EndOfEvent | the record, bit 0 set | IDLE |
| OPEN | not on port 1, EndOfEvent | END_OF_EVENT marker | IDLE |
| OPEN | has matchEventIndicator, no EndOfEvent, or ExecutionSummary | the record, if on port 1 | OPEN |

Consequences, all deliberate:
* The state changes only on messages. A packet gap, a duplicate, a SequenceReset or a ChannelReset does not force a close. If the packet holding an event's EndOfEvent is lost, the event stays open, and the channel's next EndOfEvent closes it (a standalone statistic then produces a marker). The stale flags (port 2) cover the book's correctness in that case.
* The records of an event and its close are always on the same channel, because B3 events never span channels.
* Every close a port 1 consumer can see appears on port 1 exactly once, as one record with flag bit 0: the closing message's own record, or its marker, never both. The only closes missing from port 1 are those with no event open and no port 1 record, which close nothing a port 1 consumer saw.

### 3.6 Which messages reach port 1

Port 1 is written in decoder order: packets in the order the decoder accepts them, then messages in packet order.

* **Accepted packets** (sequencing rules from `spec/LAYOUTS.md` 12.1, brief section 6): every packet whose sequenceNumber is the expected one or higher (a gap). The packet holding a SequenceReset is accepted without checking its own sequence fields. A packet whose sequenceNumber is at or below the last accepted one in the same sequenceVersion is a **duplicate**: it produces no records and does not touch the closing state.
* **Heartbeats** (sequenceNumber 0) produce no records.
* **Malformed packets** (project policy, not B3): the complete messages before the fault are processed normally (records, closing state). The rest of the packet is discarded. A message is complete when its 12-byte header fits in the datagram, messageLength ≥ 12, messageLength fits in the datagram, encodingType is 0xEB50 and schemaId is 2. A datagram shorter than the 16-byte packet header is discarded and uses up no sequence number.
* **Schema versions**: fields are read using the message header's blockLength and version. A field exists only if it lies within blockLength and its sinceVersion is at most the header's version. Absent fields follow section 1. Extra root-block bytes are skipped.
* **Instruments not in the book table** still get port 1 records.

### 3.7 Feeds and record order

events.bin describes one feed's pcap, decoded alone (A/B arbitration comes in step 11).

* **Feed A**: the generator's intent.jsonl is in Feed A wire order. `tools/intent_to_events.py` produces the expected events.bin for every set.
* **Feed B** carries the same packets as A (minus `missing.B`), in the same order *within each channel*. But it merges the channels by its own jittered pcap timestamps, which intent.jsonl does not record. So the cross-channel order of a Feed B events.bin cannot be derived from the answer keys, and the converter refuses `--feed B`. Per channel, Feed B's records equal Feed A's records with `missing.B` removed instead of `missing.A`. A Feed B check needs either the generator to record B's packet order, or a comparison done per channel.
* `tools/compare_decode.py` matches rows per channel, so it checks a decode of either feed.

### 3.8 Not on port 1, but counted

Statistics, News, heartbeats and unknown templates appear in decode.jsonl and the counters JSON. Duplicates and discarded malformed tails appear only in the counters. Neither file is specified here.

### 3.9 Worked examples

The bytes below are produced by `tools/intent_to_events.py`, and `tools/tests/test_design_examples.py` checks them against this file. Each record is 64 bytes, shown as four rows of 16.

#### Example 1: B3's sample packet (Message Reference section 6)

Input: `feed_generator/tests/fixtures/b3_sample_packet/`. Channel 55, sequenceVersion 1, sequenceNumber 987654321. Message 0 is Order_MBO CHANGE (mDUpdateAction 1), offer, matchEventIndicator 0x80. Message 1 is Trade, matchEventIndicator 0.

| Record | Field values |
|---|---|
| 0 MODIFY | seq 987654321 (`b1 68 de 3a`), version 1, channel 0x37, index 0, kind 2, flags 0x01 (EndOfEvent), side 2 (offer), type 50 (0x32), rptSeq 0, securityID 100000109220, price 12345678, qty 10000, order_id 0, exch_time 1579546260000000000, aux = mDEntryPrevSize 0 |
| 1 TRADE | index 1, kind 8, flags 0, side 0, type 53 (0x35), same securityID, price and qty, order_id = tradeID 0, aux = buyer 1234 << 32 \| seller 5678 = `0x000004d20000162e` |

The Trade has no EndOfEvent, so it leaves the channel's event OPEN. The packet ends there, so no marker follows.

<!-- example:sample_packet -->
```text
# record 0: MODIFY
b1 68 de 3a 01 00 37 00 02 01 02 32 00 00 00 00
a4 92 78 48 17 00 00 00 4e 61 bc 00 00 00 00 00
10 27 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 c8 98 65 f4 ac eb 15 00 00 00 00 00 00 00 00
# record 1: TRADE
b1 68 de 3a 01 00 37 01 08 00 00 35 00 00 00 00
a4 92 78 48 17 00 00 00 4e 61 bc 00 00 00 00 00
10 27 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 c8 98 65 f4 ac eb 15 2e 16 00 00 d2 04 00 00
```

#### Example 2: a trade event closed by ExecutionStatistics

Input: `tools/tests/fixtures/trade_event_intent.jsonl`, four messages copied from the generator's `small` set (seed 1). PETR4 (securityID 100000001), channel 70, sequenceVersion 1, sequenceNumber 16. An aggressive sell of 100 at 38.51 fills a resting bid of 300, leaving 200.

| msg | Template | matchEventIndicator | Port 1 | State after |
|---|---|---|---|---|
| 0 | ExecutionSummary (55) | none | EXEC_SUMMARY: lastPx 385100, fillQty 100, rptSeq 26, aux = aggressorSide 2 | OPEN |
| 1 | Trade (53) | 0x00 | TRADE: px 385100, qty 100, tradeID 3, rptSeq 27, aux = 15 << 32 \| 14 | OPEN |
| 2 | Order_MBO CHANGE (50) | 0x00 | MODIFY: bid, px 385100, qty 200, order 1000000000007, rptSeq 28, aux = prevSize 300 | OPEN |
| 3 | ExecutionStatistics (56) | 0x80 | END_OF_EVENT: type 56, flags 0x01, exch_time = mDEntryTimestamp | IDLE |

All four messages carry exch_time 1792414800042076781 (`6d 2a a9 96 c3 ef df 18`).

<!-- example:trade_event -->
```text
# record 0: EXEC_SUMMARY
10 00 00 00 01 00 46 00 07 00 00 37 1a 00 00 00
01 e1 f5 05 00 00 00 00 4c e0 05 00 00 00 00 00
64 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
6d 2a a9 96 c3 ef df 18 02 00 00 00 00 00 00 00
# record 1: TRADE
10 00 00 00 01 00 46 01 08 00 00 35 1b 00 00 00
01 e1 f5 05 00 00 00 00 4c e0 05 00 00 00 00 00
64 00 00 00 00 00 00 00 03 00 00 00 00 00 00 00
6d 2a a9 96 c3 ef df 18 0e 00 00 00 0f 00 00 00
# record 2: MODIFY
10 00 00 00 01 00 46 02 02 00 01 32 1c 00 00 00
01 e1 f5 05 00 00 00 00 4c e0 05 00 00 00 00 00
c8 00 00 00 00 00 00 00 07 10 a5 d4 e8 00 00 00
6d 2a a9 96 c3 ef df 18 2c 01 00 00 00 00 00 00
# record 3: END_OF_EVENT (closing message: ExecutionStatistics, msg 3)
10 00 00 00 01 00 46 03 0c 01 00 38 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
6d 2a a9 96 c3 ef df 18 00 00 00 00 00 00 00 00
```

---

## 4. Port 2: book updates

### 4.1 Record layout

A 32-byte header, then N bid levels and N offer levels, best first, then zero padding.

**Record size** = 32 + 2 × N × 24, rounded up to a multiple of 64. N = 5: 272 → **320 bytes**. (N = 1: 128, N = 10: 512.)

Header:

| Off | Size | Type | Field | Meaning |
|---|---|---|---|---|
| 0 | 4 | u32 | seq_num | Identity of the message that closed the event (3.5) |
| 4 | 2 | u16 | seq_version | |
| 6 | 1 | u8 | channel | |
| 7 | 1 | u8 | msg_index | |
| 8 | 8 | u64 | instrument_id | securityID |
| 16 | 8 | u64 | exch_time | Closing message's time field (3.4) |
| 24 | 1 | u8 | flags | Below |
| 25 | 1 | u8 | bid_count | Valid bid levels, 0..N |
| 26 | 1 | u8 | offer_count | Valid offer levels, 0..N |
| 27 | 5 | | reserved | 0 |

Levels: bid level i (0 = best) at offset 32 + 24·i, offer level i at 32 + 24·(N + i). Padding from 32 + 48·N to the record size is 0.

| Off in level | Size | Type | Field |
|---|---|---|---|
| 0 | 8 | i64 | price |
| 8 | 8 | i64 | qty (total at the level) |
| 16 | 4 | u32 | order_count |
| 20 | 4 | | reserved, 0 |

* Bids are sorted by price descending, offers ascending. Levels at index ≥ bid_count (or offer_count) are all zero.
* A level with order_count 0 is never published (the slot is removed when its count reaches 0).

Flags:

| Bit | Flag | Meaning |
|---|---|---|
| 0 | STALE | The instrument's book is stale (brief section 5). Under the v1 publish rule (no updates for stale instruments) it is always 0. It is kept for a later mode that publishes stale books |
| 1 | RECOVERY | A record that changed this instrument's book in the event had RecoveryMsg |
| 2 | DEPTH_INCOMPLETE_BID | Bid side truncated with fewer than N valid levels (brief section 5) |
| 3 | DEPTH_INCOMPLETE_OFFER | Same, offer side |
| 4–7 | reserved, 0 | |

### 4.2 Capped and uncapped books

* **Capped** (`B3_BOOK=capped`, the RTL's algorithm): K slots per side, with the truncation and depth-incomplete rules of brief section 5. The file header records K.
* **Uncapped** (`B3_BOOK=uncapped`, software only): no tracked-depth limit. Same record format. The file header records **K = 0**, and DEPTH_INCOMPLETE_BID/OFFER are always 0.

### 4.3 When records are written

At each close of a channel's event (3.5), one record per instrument in `touched[ch]` that is in the book table and not stale, in order of first touch within the event. CLEAR_CHANNEL touches every instrument of that channel in the book table, in table order. An instrument is touched when it receives a book-changing record (ADD, MODIFY, DELETE, CLEAR_SIDE, CLEAR_BOOK, CLEAR_CHANNEL), even if the change falls in the untracked region and the published levels do not change. Nothing is written mid-event (brief open item E).

### 4.4 Decided in step 8

* **Instrument lookup**: how securityID maps to a book slot (runtime table, loaded from a config file) and how the RTL loads it.
* **Table-full behavior**: what happens to an instrument that does not fit in MAX_INSTRUMENTS.

---

## 5. Tools

All in `tools/`, Python standard library only, tests in `tools/tests/` (pytest). None of them imports b3gen.

| Tool | Does |
|---|---|
| `b3contract.py` | The layouts of sections 2 to 4: pack and unpack |
| `b3intent.py` | Reads intent.jsonl and manifest.json: the rows a decoder of one feed sees (`missing`, malformed `faults`) |
| `intent_to_events.py` | intent.jsonl → expected events.bin (Feed A) |
| `compare_decode.py` | decode.jsonl vs intent.jsonl, field for field, including null vs absent. First mismatches with packet and message position |
| `dump_events.py` | events.bin → readable rows (`--hex` for raw bytes) |

```sh
python3 tools/intent_to_events.py data/small/intent.jsonl -o expected/small_events.bin
python3 tools/dump_events.py expected/small_events.bin --limit 20
python3 tools/compare_decode.py out/small/decode.jsonl data/small/intent.jsonl
python3 -m pytest tools/tests
make data && python3 -m pytest tools/tests   # data tests fail until make data has run
```

`intent_to_events.py` writes N, K and MAX_INSTRUMENTS into the header (defaults 5, 16, 64, matching `book_params.hpp`). For a golden file matched against another build, pass `--tracked-depth 32` (or `0` for uncapped) and so on.

`compare_decode.py` compares `instrument` and `news` only when the decode row has them (they need the instrument table and News reassembly). `--strict` requires them.
