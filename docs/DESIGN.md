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
| 3 | BOOK_NOT_CLEAN | The record's instrument is a table instrument (4.4) whose state is not CLEAN (today: STALE, 3.6.4: a loss, an rptSeq jump or a silence) once this message has been applied: a gap or a silence is marked before the packet that reveals it, so its records carry the bit; an rptSeq jump marks its own message; an instrument's EmptyBook and a ChannelReset clear it first, so their records do not. Records with no table instrument (channel-wide SESSION, CLEAR_CHANNEL, SEQ_RESET, END_OF_EVENT, or a securityID not in the table) have 0 |
| 4–7 | reserved, 0 | |

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
* A **SequenceReset** or a **ChannelReset** clears the channel's event state: after its own record (SEQ_RESET or CLEAR_CHANNEL, flags as usual) the channel is IDLE and its touched set empty, whatever was open, and nothing else is emitted (no marker, no port 2 update for the cleared event).

**State machine.** One instance per channel, two states. It runs on every message the decoder accepts (3.6), in decoder order, whether or not the message is on port 1.

```
state[ch]   : IDLE | OPEN          (reset value IDLE)
touched[ch] : set of instruments   (reset value empty; used by port 2)

for each accepted message m on channel ch:
    rec  = port-1 record of m, or none                      (3.2)
    mei  = m.matchEventIndicator, or none if the template has none

    if m is SequenceReset or ChannelReset:
        emit rec                                            (SEQ_RESET / CLEAR_CHANNEL)
        state[ch] = IDLE; touched[ch] = empty               (nothing else: no marker, no close)
        continue with the next message
    if rec: emit rec                                        (with flag bit 0 = mei.bit7)
    if rec changes a book: touched[ch] += rec.instrument   (ADD, MODIFY, DELETE,
                                                            CLEAR_SIDE, CLEAR_BOOK, CLEAR_CHANNEL)
    if mei is none:
        if m is ExecutionSummary: state[ch] = OPEN
        else: no change                                     (heartbeat, SecurityDefinition,
                                                             unknown template)
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
| IDLE or OPEN | SequenceReset or ChannelReset | its record (SEQ_RESET, CLEAR_CHANNEL) | IDLE, touched set empty |
| IDLE or OPEN | no matchEventIndicator (other than 55 and 1) | nothing (not on port 1) | unchanged |
| OPEN | on port 1, EndOfEvent | the record, bit 0 set | IDLE |
| OPEN | not on port 1, EndOfEvent | END_OF_EVENT marker | IDLE |
| OPEN | has matchEventIndicator, no EndOfEvent, or ExecutionSummary | the record, if on port 1 | OPEN |

Consequences, all deliberate:
* The state changes only on messages. A packet gap or a duplicate does not force a close; a SequenceReset or ChannelReset clears the state without a close (no marker). If the packet holding an event's EndOfEvent is lost, the event stays open, and the channel's next EndOfEvent closes it (a standalone statistic then produces a marker), unless a reset clears it first. The stale flags cover the book's correctness in that case.
* The records of an event and its close are always on the same channel, because B3 events never span channels.
* Every close a port 1 consumer can see appears on port 1 exactly once, as one record with flag bit 0: the closing message's own record, or its marker, never both. The only closes missing from port 1 are those with no event open and no port 1 record, which close nothing a port 1 consumer saw.

### 3.5.1 Implementations

* Python, the answer key: `tools/intent_to_events.py` (from intent.jsonl).
* C++: `cpp_baseline/include/b3/records.hpp` (the record and file header as structs, every offset `static_assert`ed, little-endian hosts only) and `event_sink.hpp` (the per-kind fields of 3.4 and the state machine above). `b3_decode --events FILE` writes the file. Its record buffer is allocated before the decode loop with one slot per complete message, counted at load time (a message gives at most one record: its own or a marker); a record past that capacity is counted, never stored.
* `make events-compare` checks the C++ against the converter on every set, both feeds byte for byte in full order (3.7), in the release, k16, k32 and uncapped builds, whose records must be identical (only the header's N and K differ). `make goldens` writes the RTL goldens (section 5).

### 3.6 Which messages reach port 1

Port 1 is written in decoder order: packets in the order the decoder accepts them, then messages in packet order.

* **Accepted packets** (sequencing rules from `spec/LAYOUTS.md` 12.1, brief section 6): every packet whose sequenceNumber is the expected one or higher (a gap). The packet holding a SequenceReset is accepted without checking its own sequence fields. A packet whose sequenceNumber is at or below the last accepted one in the same sequenceVersion is a **duplicate**: it produces no records and does not touch the closing state.
* **Heartbeats** (sequenceNumber 0) produce no records.
* **Malformed packets** (project policy, not B3): the complete messages before the fault are processed normally (records, closing state). The rest of the packet is discarded. Which messages are complete is defined exactly in 3.6.1. A datagram shorter than the 16-byte packet header is discarded and uses up no sequence number.
* **Schema versions**: fields are read using the message header's blockLength and version. A field exists only if it lies within blockLength and its sinceVersion is at most the header's version. Absent fields follow section 1. Extra root-block bytes are skipped.
* **Instruments not in the book table** still get port 1 records.

### 3.6.1 Packet and message framing

What the packet decoder and message walker do with each UDP payload, in order (`cpp_baseline/include/b3/packet.hpp`, `walker.hpp`). Counter names are those of `b3_decode`.

1. **Short datagram**: fewer than 16 bytes. Discarded and counted (`short_datagrams`). It never reaches sequencing, so it uses up no sequence number.
2. **Oversize packet**: more than 1400 bytes, B3's maximum (`spec/LAYOUTS.md` 2). Counted (`oversize_packets`) and otherwise decoded like any packet. A size over B3's limit says nothing about which bytes are wrong, and the message test below still guards every message.
3. **Header-only packet**: exactly 16 bytes. A valid packet with no messages, counted (`empty_packets`), not malformed. It has a packet header, so it takes part in sequencing (step 6) like any other packet.
4. **The walk**: from byte 16, one message after another, advancing by messageLength only. Each message must pass the **complete message test**. The tests run in this order, and the first failure names the reason:

| # | Test | Reason when it fails |
|---|---|---|
| 1 | At least 12 bytes remain (the message header fits) | `header_truncated` |
| 2 | messageLength ≠ 0 | `length_zero` |
| 3 | messageLength ≥ 12 | `length_too_small` |
| 4 | messageLength ≤ bytes remaining in the datagram | `length_past_end` |
| 5 | encodingType = 0xEB50 | `bad_encoding` |
| 6 | schemaId = 2 | `bad_schema_id` |
| 7 | blockLength ≤ messageLength − 12 | `block_length_past_end` |

   On the first failure the decoder counts the packet once under that reason (`malformed`, `malformed_packets`), stops and discards the rest of the packet. Messages already passed on from that packet stand. The packet itself stays accepted, so sequencing carries on. Tests 2 and 3 are one rule (messageLength ≥ 12), split so that a zero length, which a naive walker would loop on, has its own counter.
5. **Leftover bytes**: 1 to 11 bytes after the last complete message are malformed (test 1, `header_truncated`), not padding. B3 puts no padding between messages (`spec/LAYOUTS.md` 2).
6. **Unknown templateId**: a complete message whose templateId is not in the schema is skipped by its messageLength and counted (`unknown_templates`). It is not malformed. The header's version is not checked at this stage; fields are read by blockLength and version (3.6).
7. **msg_index** counts complete messages from 0 within each packet, unknown templates included, the same as intent.jsonl's `index`.
8. **msg_index overflow**: port 1 stores msg_index in one byte, so a message that would get index 256 or more is not passed on. The packet is counted once (`msg_index_overflow`, not malformed) and the rest of it discarded. A real feed cannot get there: the Ethernet MTU caps a UDP payload at 1472 bytes, which holds at most (1472 − 16) / 12 = 121 messages, and B3's own limit is 1400 bytes.

Groups and var data are not walked here: messageLength already covers them. Bounds inside a message (group entries, var data lengths) are checked when its fields are decoded (3.6.2).

### 3.6.2 Message body: fields, groups, var data

What the field decoder does with one complete message (`cpp_baseline/include/b3/decode.hpp`; descriptors in the generated `fields.hpp`).

**Root-block fields.** A field is **present** when offset + size ≤ the header's blockLength and its sinceVersion ≤ the header's version. Otherwise it is **absent**: the message's version does not have it. A present field whose wire value equals its schema null is **null**. Null and absent are different answers and are reported separately, as intent.jsonl does (`null`, `absent`). Bytes of the root block past the last known field (a newer version's additions) are skipped. Constant fields take their value from the schema, never from the wire. A char[N] value is its bytes up to the first trailing NUL; an optional char or char[N] with no characters is null.

**Groups** start at offset 12 + blockLength of the message header, not at the schema's block size. Each group has a dimension header (blockLength uint16, numInGroup uint8), then numInGroup entries of the **wire's** entry blockLength. Entry fields follow the same presence rule, with the entry blockLength in place of the root's. **Var data** follows the groups, each a length prefix (its size from the schema) and that many bytes. Bytes after the last var data, still inside messageLength, are skipped.

**Body faults.** The message framing (3.6.1) is intact, but a group or var data field runs past messageLength. Every read is checked against messageLength, in wire order:

| # | Check | Reason when it fails |
|---|---|---|
| 1 | The group's dimension header fits | `group_header_past_end` |
| 2 | Each entry, at the wire's entry blockLength, fits | `group_entry_past_end` |
| 3 | The var data length prefix fits | `var_data_length_past_end` |
| 4 | The var data bytes fit | `var_data_past_end` |

On the first failure the decoder counts it by reason, keeps what it decoded before it (the root block, earlier groups, the entries that fit, earlier var data), stops decoding that message, and **carries on with the next message**: messageLength still frames it. The full decode row gets `"body_fault": {"reason", "at"}` (`at` names the group or var data field) and `"undecoded"`: that group or field and every later one. A News part with a body fault produces no reassembled `news` and ends the reassembly open on its channel.

**Body faults are a software-only counter.** Groups and var data are walked only by the software counters (`b3_decode --counters`) and the full decode (`--decode`). The decode path that is timed (step 10), like the FPGA, never reads a message past its root block: a message's fields are read at their fixed offsets inside blockLength, and the walker steps over the rest by messageLength. No port 1 record or event-closing decision needs a group or var data field.

**Full decode format** (`b3_decode --decode`). One JSON line per message, in the shape of intent.jsonl, values as intent.jsonl writes them: numbers for integers, enums, sets, prices (raw mantissas) and timestamps; strings for char, char[N] (`""` for NUL) and var data; `"news"` on a News's last part with the parts' text joined (parts 1 to partCount, in order, on one channel); `{"body": "<hex>"}` for an unknown template. No `instrument`: it is a name from the generator's config. Two formats intent.jsonl has no precedent for (the generator never writes groups or multi-member composites):

* a **group** is a list of entries, each `{"fields": {...}, "null": [...], "absent": [...]}`
* a **multi-member composite** (MaturityMonthYear) is an object of its members' wire values, listed in `null` when optional and every member holds its null

### 3.6.3 Sequencing

Per channel, on every packet that passed the packet decoder (`cpp_baseline/include/b3/packet.hpp`, `Sequencer`), following `spec/LAYOUTS.md` 12.1 and `feed_generator/tests/book_builder.py`. A short datagram never gets here (3.6.1); a malformed packet does, so it uses up its sequence number. State is a fixed array of 256 channels, each: `seen`, `reset_pending`, `version` (u16), `next` (u32, the expected sequenceNumber), and a ring of its last 8 gaps (version, first, last).

**The decision is made once per packet, before any message is dispatched, from two things only**: the 16-byte packet header, and the header of the packet's first message. The first message makes a **reset packet** when it passes the complete message test (3.6.1) and its templateId is 1 (SequenceReset). Nothing else in the packet is looked at, so the RTL can decide while the first message header streams in.

| Packet (checked in this order) | Rule |
|---|---|
| sequenceNumber 0 | **Heartbeat**: not sequenced, accepted. Its nextSeqNo is checked when its Sequence message is dispatched (below). If its first message is not a complete Sequence, `heartbeat_without_sequence` is counted |
| Reset packet | Accepted without checking its own sequenceNumber or version. `reset_pending` is set |
| `reset_pending` is set | Cleared. The packet should be (version + 1, 1), with version 65534 followed by 1; if not, a **reset mismatch** is counted. The channel then expects (version + 1, 1) and the packet is judged by the rows below, so a higher number is a gap |
| First sequenced packet on the channel | Accepted. Sets version and `next` = its sequenceNumber + 1 |
| sequenceVersion ≠ the channel's | Accepted, counted as a **version change**, and followed (sets version and `next`) |
| sequenceNumber = `next` | Accepted |
| sequenceNumber > `next` | Accepted. A **gap** of sequenceNumber − `next` packets is counted, starting at `next` (the stale point, 3.6.4) |
| sequenceNumber < `next`, inside a gap this channel recorded | **Late** (`late_after_gap`): dropped like a duplicate (no dispatch, no state touched), but counted apart. The channel remembers its last 8 gaps (a fixed ring per channel, same sequenceVersion only); a late packet for an older gap is counted as a duplicate |
| sequenceNumber < `next`, otherwise | **Duplicate**: dropped. Not dispatched, so it touches no counter, event state, stale flag or book |

On dispatch (`cpp_baseline/include/b3/tracker.hpp`):

* a **Sequence** message in a heartbeat packet: nextSeqNo must equal the channel's `next`, or 1 when `reset_pending` is set; otherwise a **heartbeat mismatch** is counted. Before the channel's first sequenced packet there is nothing to check.
* a **SequenceReset** that is not its packet's first message is counted (`seq_reset_not_first`) and is otherwise an ordinary message: it does not make a reset packet.

### 3.6.4 rptSeq and stale flags

Per table instrument (4.4), on dispatch (`Tracker`). Flags only: there is no book yet (step 8 adds it).

* **rptSeq**: every message with a securityID in the table and an rptSeq field. It must be the previous + 1, or 1 on the message after the instrument's EmptyBook. A jump is counted (`rpt_seq_gaps`) and marks **that instrument** stale, at the packet holding the message. The first message seen sets the starting value. A ChannelReset does not touch rptSeq; in every generator set each instrument's first message after a ChannelReset (or a SequenceReset) is its own EmptyBook, which restarts it.
* **Losses** mark **every table instrument of the channel** stale: a sequence gap (stale point: channel, sequenceVersion and the first missing sequenceNumber) and a discarded tail (a malformed packet, or messages past msg_index 255; stale point: the packet itself). A decoder cannot know what the lost bytes held, so a loss is assumed to have held a message for each instrument of the channel, or a channel-wide one. A gap is marked before the packet that reveals it is dispatched; a discarded tail after the packet's complete messages.
* **Silence** (`spec/LAYOUTS.md` 12.1: no message for 3 heartbeat intervals means the stream is down): each channel's last datagram is remembered (any datagram with a packet header: a heartbeat, a duplicate, a late packet). When time next advances (a datagram arrives, on any channel), every channel that has sent nothing for more than the **silence limit** (3 s of capture time by default, `b3_decode --silence-ns`, 0 turns it off) marks every CLEAN table instrument of it STALE, once per silence. Stale point: (channel, its sequenceVersion, its next expected sequenceNumber). The check runs before the datagram that advanced time is looked at, so that datagram's own channel is judged by its previous one. Counted (`silences`).
* **Clearing**: an instrument's **EmptyBook** clears its stale flag; a **ChannelReset** clears it for every table instrument of its channel. Nothing else does.
* **Before the rebuild**: after a ChannelReset, a message for a table instrument of that channel arriving before the instrument's own EmptyBook is counted (`msg_before_rebuild`). B3 may interleave such messages (generator open item J); the generator never does, and no rule here depends on it.
* Reported per instrument: rptSeq gaps, the number of times it went stale, whether it is stale at the end, and its **first stale point** (channel, sequenceVersion, sequenceNumber), kept even after a clear.
* **No snapshot recovery** in v1. A loss with no later EmptyBook or ChannelReset leaves the instruments stale to the end of the file.
* securityID not in the table: counted (`unknown_instrument_messages`), not tracked. A null securityID (a market-wide News) is not counted.

The generator's `unreliable_books` (manifest.json) comes from the contents of the lost packets and covers packets missing from both feeds only. The rule above uses only what a decoder sees, so it flags at least as much and at the same point or earlier: `tools/counters_compare.py` derives the decoder's expected flags independently and also checks every `unreliable_books` entry against them.

### 3.7 Feeds and record order

events.bin describes one feed's pcap, decoded alone (A/B arbitration comes in step 11).

* The answer keys follow **each feed's own packet order and capture times**, read from its pcap with the generator's independent reader (`tools/b3feed.py`): per channel, the next packet of the feed's view of intent.jsonl (`tools/b3intent.py`) is the next datagram of that channel the decoder accepts; the messages themselves are the intent log's. `tools/intent_to_events.py` (port 1) and `tools/expected_book.py` (port 2) produce the expected files for **both feeds**, and both are compared byte for byte in full order. The capture times also drive the silence rule (3.6.4) and STATUS (4.3).
* **Feed B** carries the same packets as A (minus `missing.B`), in the same order within each channel, merged across channels by its own jittered pcap timestamps.
* In the `reorder` scenario the keys follow the reorder window's release order (`docs/CPP_BASELINE_CONTEXT.md` section 6), which is not a pcap's order: an expected failure until step 11, whose port 1 Feed A key is the view's order without capture times.
* `tools/compare_decode.py` matches rows per channel, so it checks a decode of either feed.

### 3.8 Not on port 1, but counted

Statistics, News, heartbeats and unknown templates appear in decode.jsonl and the counters JSON. Duplicates and discarded malformed tails appear only in the counters. Neither file is specified here.

### 3.9 Worked examples

The bytes below are produced by `tools/intent_to_events.py`, and checked against this file twice: by `tools/tests/test_design_examples.py` (the Python converter) and by `cpp_baseline/tests/test_events.cpp` (the C++ event sink, which parses the blocks from this file the same way). This file is the shared ground truth; the RTL's testbench is meant to read the same blocks. Each record is 64 bytes, shown as four rows of 16.

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

Every port 2 record has the same size: a 48-byte header, then N bid levels and N offer levels, best first, then zero padding. Levels a record does not use are zero.

**Record size** = 48 + 2 × N × 24, rounded up to a multiple of 64. N = 5: 288 → **320 bytes** (unchanged from the earlier 32-byte header). N = 1: 128, N = 10: 576.

Header:

| Off | Size | Type | Field | Meaning |
|---|---|---|---|---|
| 0 | 8 | u64 | out_seq | Output sequence number: 1 on the file's first record, + 1 on each record, so a consumer sees a missing record |
| 8 | 4 | u32 | seq_num | Identity of the message the record is for (4.3), 0 on STATUS |
| 12 | 2 | u16 | seq_version | |
| 14 | 1 | u8 | channel | |
| 15 | 1 | u8 | msg_index | |
| 16 | 8 | u64 | instrument_id | securityID, 0 on STATUS |
| 24 | 8 | u64 | exch_time / ticks_skipped | BOOK_UPDATE and STATE_CHANGE: the message's time field (3.4), 0 on a loss's or a silence's STATE_CHANGE. STATUS: **ticks_skipped**, the number of earlier ticks this STATUS stands for |
| 32 | 8 | u64 | capture_time | Capture time (pcap, ns) of the packet being processed; on STATUS, the interval's tick |
| 40 | 1 | u8 | record_type | 1 BOOK_UPDATE, 2 STATE_CHANGE, 3 STATUS (0 never written) |
| 41 | 1 | u8 | state | The instrument's book state after the record (below); 0 on STATUS |
| 42 | 1 | u8 | flags | Below |
| 43 | 1 | u8 | bid_count | Valid bid levels, 0..N |
| 44 | 1 | u8 | offer_count | Valid offer levels, 0..N |
| 45 | 1 | u8 | cause | STATE_CHANGE: what caused it (below); 0 on other record types |
| 46 | 2 | | reserved | 0 |

Levels: bid level i (0 = best) at offset 48 + 24·i, offer level i at 48 + 24·(N + i). Padding from 48 + 48·N to the record size is 0.

| Off in level | Size | Type | Field |
|---|---|---|---|
| 0 | 8 | i64 | price |
| 8 | 8 | i64 | qty (total at the level) |
| 16 | 4 | u32 | order_count |
| 20 | 4 | | reserved, 0 |

* Bids are sorted by price descending, offers ascending. Levels at index ≥ bid_count (or offer_count) are all zero.
* A level with order_count 0 is never published (it is removed when its count reaches 0).

**Book state** (byte 41, per table instrument):

| Value | State | Meaning |
|---|---|---|
| 0 | CLEAN | The book can be trusted |
| 1 | HOLDING | Reserved for step 11 (a hole held in the arbitration window) |
| 2 | STALE | A loss, an rptSeq jump or a silence (3.6.4); cleared by the instrument's EmptyBook or a ChannelReset |
| 3 | RECOVERING | Reserved for step 12 (snapshot recovery) |

**Cause** (byte 45, STATE_CHANGE only; 0 on BOOK_UPDATE and STATUS):

| Value | Cause | Transition |
|---|---|---|
| 1 | Gap | CLEAN → STALE: missing packets (3.6.1), before the packet that reveals them |
| 2 | Discarded tail | CLEAN → STALE: a malformed packet's tail was discarded (3.2), after its complete messages |
| 3 | rptSeq jump | CLEAN → STALE: this instrument's rptSeq skipped (3.6.4) |
| 4 | Silence | CLEAN → STALE: the channel sent nothing for more than the silence limit (3.6.4) |
| 5 | EmptyBook | STALE → CLEAN: the instrument's EmptyBook |
| 6 | ChannelReset | STALE → CLEAN: its channel's ChannelReset |

0 and 7–255 are never written on a STATE_CHANGE.

**Flags** (byte 42):

| Bit | Flag | Meaning |
|---|---|---|
| 0 | RECOVERY | BOOK_UPDATE: a record that changed this instrument's book in the event had RecoveryMsg. 0 on other types |
| 1 | DEPTH_INCOMPLETE_BID | Bid side truncated with fewer than N valid levels (brief section 5); always 0 in the uncapped book |
| 2 | DEPTH_INCOMPLETE_OFFER | Same, offer side |
| 3–7 | reserved, 0 | |

**File header** (section 2): magic `B3PORT2\0`, the record size, N, K (0 for the uncapped book), MAX_INSTRUMENTS, and in book.bin the 8 bytes at offset 24 hold the **STATUS interval** in ns (events.bin keeps them 0).

### 4.2 Capped and uncapped books

* **Capped** (`B3_BOOK=capped`, the default, the RTL's algorithm, `cpp_baseline/include/b3/book_capped.hpp`): K slots per side, with the truncation and depth-incomplete rules below (brief section 5). The file header records K. Step 8b.
* **Uncapped** (`B3_BOOK=uncapped`, software only, step 8a, 4.5): no tracked-depth limit. Same record format and emission rules. The file header records **K = 0**, and DEPTH_INCOMPLETE_BID/OFFER are always 0.

**The capped book.** Per table instrument and side: up to K slots (price, qty, order_count) sorted best first, a valid count, a `truncated` flag and a boundary price B. **Invariant:** the slots hold every level at or better than B; if not truncated, they hold the whole side.

| Port 1 record | Capped book |
|---|---|
| ADD at price p | The level is in the slots: qty += qty, count += 1. Otherwise, if not truncated or p is at or better than B: insert in sorted position; if that makes K + 1 levels, the worst is dropped, `truncated` is set, and B becomes the new worst slot's price. Truncated and p worse than B: ignored |
| MODIFY, DELETE | The level is in the slots: applied as in 4.5 (a DELETE removes the slot at count 0, **B unchanged**). Not in the slots: worse than B while truncated, ignored; otherwise (at or better than B, or not truncated) a **consistency error** |
| CLEAR_SIDE, CLEAR_BOOK, CLEAR_CHANNEL | No slots, `truncated` false |

"Better" is a higher price for bids, a lower one for offers. Null prices are counted and kept out of the slots, as in 4.5; consistency errors are split CLEAN / STALE, as in 4.5.

**Depth incomplete:** a side is depth incomplete when it is truncated and has fewer than N valid slots (unknown levels beyond B might belong in the published N). It ends when the side is cleared, or when valid slots reach N again; `truncated` itself ends only with a clear.

**Port 2 with the capped book.** The same format and emission rules (4.1, 4.3). A record's levels are the side's first min(N, valid) slots, always correct. BOOK_UPDATE and STATE_CHANGE set DEPTH_INCOMPLETE_BID / DEPTH_INCOMPLETE_OFFER (byte 42, bits 1 and 2) from the instrument's sides at the moment the record is written (on a STATE_CHANGE to STALE too, whose levels are zeros). **A change of depth completeness writes no record of its own**: it shows on the next record for that instrument (in practice the BOOK_UPDATE of the event that caused it). Where a side is not depth incomplete, its levels equal the uncapped book's; `make book-oracle` checks exactly that.

**Statistics** (counters JSON, `book.sides.bid` / `book.sides.offer`, summed over instruments): `truncations` (truncated set from false), `levels_dropped`, `ignored` (updates worse than B while truncated), `depth_incomplete` (episodes), `depth_incomplete_ns` and `depth_incomplete_max_ns` (their capture-time duration, total and longest; an episode open at the end of the file ends at the last datagram's time), `max_valid`. `book.state_bytes` is the book's state size; the uncapped build reports its own, and `book.max_depth` (most levels per side). The RTL layout is one wide word per (instrument, side): the K slots, valid, truncated and B. An update reads it, compares the price against all K slots at once, and writes it back shifted.

### 4.3 When records are written

Port 2 is computed from port 1's records (3), the tracker's state changes (3.6.4) and the capture time; it never reads the wire. Per channel, the **touched set** holds the table instruments that received a book-changing record (ADD, MODIFY, DELETE, CLEAR_SIDE, CLEAR_BOOK) since the channel's last close, in order of first touch. Records of instruments not in the table change no book and touch nothing.

For each message, in decoder order, the book does three things, in this order:

1. **Apply** its port 1 record, if any, to the book (4.5) and update the touched set.
2. **STATE_CHANGE** for every state change the message caused (in table slot order).
3. **Close**, if its record carries flag bit 0 (EndOfEvent, a marker included) and is not a reset: BOOK_UPDATEs, then the touched set is emptied.

A loss (a gap, before the packet that reveals it; a discarded tail, after the packet's complete messages) and a silence (when time advances) cause STATE_CHANGEs only. For each datagram, in order: STATUS (if a tick is due), the silence rule's STATE_CHANGEs, then the datagram (a gap's STATE_CHANGEs, its messages, a discarded tail's).

| Record | Written when | Identity, exch_time | state | Levels |
|---|---|---|---|---|
| BOOK_UPDATE | At a close, one per touched instrument, in order of first touch, whose state is CLEAN. While STALE nothing is written, unless the build sets `B3_PUBLISH_STALE`: then it is written with state STALE | The closing record's (a marker's identity is its closing message's) | CLEAN (or STALE) | The book's top N |
| STATE_CHANGE | The moment an instrument's state changes: CLEAN → STALE on a loss, an rptSeq jump or a silence (3.6.4); STALE → CLEAN on its EmptyBook or a ChannelReset. Even with no book change | The causing message's identity and time field; for a loss or a silence, (channel, sequenceVersion, the stale point's sequenceNumber, msg_index 0) and exch_time 0 | The new state; cause at offset 45 (4.1) | The book's top N if the new state is CLEAN, else zeros |
| STATUS | Ticks at T0 + k × interval, k ≥ 1 (default 100 ms, `b3_decode --status-interval-ns`), T0 = the capture time of the file's first datagram. When a datagram's capture time reaches one or more ticks not yet written, **one** STATUS for the latest of them, before the datagram; ticks_skipped = how many earlier ticks it stands for (none after the last datagram) | All 0, instrument_id 0; capture_time = the tick; offset 24 = ticks_skipped | 0 | Zeros |

**Resets** (3.5): a SequenceReset or ChannelReset closes nothing. Its record clears the channel's touched set, discarding the open event without a BOOK_UPDATE. A ChannelReset then clears every table instrument's book on its channel and **touches** each of them, so the channel's next close publishes their (empty or rebuilt) books. Each that was STALE gets a **STATE_CHANGE to CLEAN at the reset**, carrying its empty book (bid_count and offer_count 0); the ones already CLEAN get none. Nothing is written mid-event (brief open item E).

**Live versus replay time.** `b3_decode` and the answer keys replay a capture: time exists only as packets' capture timestamps, so the STATUS ticks and the silence check run only when a datagram arrives (on any channel). If every channel goes quiet at once, nothing runs until the next datagram, and the STATUS and silence records for the outage appear only then (as one coalesced STATUS and the STATE_CHANGEs). A live implementation must run both on its own clock (the RTL: a free-running timer), so that a whole-feed outage is still detected and reported while it lasts. The goldens stay valid for the RTL because its testbench drives time from the capture timestamps, as the replay does.

Hex examples: 4.7.

### 4.4 Instrument table

The instruments the decoder tracks (rptSeq and stale flags now, books from step 8), as a plain-text file loaded once at startup, outside timed code (`b3_decode --instruments FILE`, `cpp_baseline/include/b3/instruments.hpp`).

* **Format**: one instrument per line, `securityID channel`, both decimal. `#` starts a comment, blank lines are ignored. An instrument's **slot** is its position among the instrument lines, from 0. `tools/gen_instruments.py` writes the file from the feed generator's `config.toml`.
* **Load errors** (the decoder refuses to start, naming the line): a line that is not two unsigned integers, a channel above 255, a securityID already in the table, or more instruments than MAX_INSTRUMENTS. So the table-full case never reaches the decoder: a table that does not fit is rejected whole.
* **Lookup**: the software scans the table in order and takes the matching slot. The RTL does the same lookup as one parallel compare of the securityID against every entry. securityIDs are unique, so both find the same slot.
* **Not in the table**: the message is still decoded and still goes to port 1 (3.6), but its instrument gets no rptSeq tracking and no book. It is counted.
* The channel column says which instruments a loss on a channel marks stale, and which a ChannelReset clears (3.6.4). The lookup itself is by securityID only.

### 4.5 Uncapped book

`cpp_baseline/include/b3/book_uncapped.hpp`. Per table instrument and side, a sorted flat array of levels (price, qty, order_count), the best price at the end, with a fixed capacity per side reserved at startup (`b3_decode --book-levels`, default 1024). A new level past it is counted (`level_overflow`) and dropped; the array never grows in the decode loop. The state size in bytes is in the counters JSON.

| Port 1 record | Update |
|---|---|
| ADD at price p | Level p: qty += qty, count += 1; a new level if none |
| MODIFY | Level p: qty += qty − previous qty (aux) |
| DELETE | Level p: qty −= qty, count −= 1; the level is removed when count reaches 0 |
| CLEAR_SIDE | One side emptied |
| CLEAR_BOOK | Both sides emptied |
| CLEAR_CHANNEL | Every table instrument of the channel emptied |

Trades never touch the book. A **null price** (INT64_MIN, a market-on-auction order) is counted (`null_price_orders`) and kept out of the levels. **Consistency errors** are counted, split by the instrument's state when they happen: a MODIFY or DELETE on a level that does not exist (ignored), a qty or count going negative, a level reaching count 0 with qty not 0. While CLEAN there must be none; while STALE they are expected (the lost messages built the levels).

### 4.6 Future items

* **Snapshot recovery**: rebuilding a stale instrument from the snapshot recovery stream (`spec/LAYOUTS.md` 12.6, 12.8), which would clear its stale flag. Not in v1: a stale instrument stays stale until its EmptyBook or a ChannelReset (3.6.4).

### 4.7 Worked example

The bytes below are produced by `tools/expected_book.py` (`run_packets`) from `tools/tests/book_example.py`, and checked against this file by `tools/tests/test_book_examples.py` (Python) and `cpp_baseline/tests/test_book.cpp` (the C++ book sink, which builds the same messages and parses this block the same way). N = 5: each record is 320 bytes, shown as twenty rows of 16.

Input: channel 68, WIN (securityID 200000001) the only table instrument, STATUS interval 100 ms.

| Packet | Capture time | Messages | Port 2 |
|---|---|---|---|
| 1 | 1.000 s (T0) | Order_MBO NEW bid 1299800000 x5 (soid 1, rptSeq 1, mEI 0); Order_MBO NEW bid 1299750000 x3 (soid 2, rptSeq 2, mEI 0x80, transactTime 999000100) | BOOK_UPDATE, out_seq 1 |
| 2 | | missing | |
| 3 | 1.050 s | Order_MBO NEW offer 1299850000 x2 (rptSeq 4, mEI 0x80) | STATE_CHANGE to STALE (the gap, before the packet), out_seq 2; the order is booked but, STALE, not published |
| 4 | 1.150 s | EmptyBook WIN (mEI 0x20, mDEntryTimestamp 1149000000) | STATUS for the 1.100 s tick (before the packet, ticks_skipped 0), out_seq 3; STATE_CHANGE to CLEAN, out_seq 4 |

In v1 a STATE_CHANGE to CLEAN always shows an empty book: both events that clear STALE (EmptyBook, ChannelReset) empty it.

<!-- example:book_records -->
```text
# BOOK_UPDATE: WIN bids 1299800000 x5 (1), 1299750000 x3 (1), closing message packet 1 message 1
01 00 00 00 00 00 00 00 01 00 00 00 01 00 44 01
01 c2 eb 0b 00 00 00 00 24 88 8b 3b 00 00 00 00
00 ca 9a 3b 00 00 00 00 01 00 00 02 00 00 00 00
c0 5f 79 4d 00 00 00 00 05 00 00 00 00 00 00 00
01 00 00 00 00 00 00 00 70 9c 78 4d 00 00 00 00
03 00 00 00 00 00 00 00 01 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
# STATE_CHANGE to STALE: the gap at packet 2 (identity 68/1/2/0), cause 1 (gap), levels zero
02 00 00 00 00 00 00 00 02 00 00 00 01 00 44 00
01 c2 eb 0b 00 00 00 00 00 00 00 00 00 00 00 00
80 ba 95 3e 00 00 00 00 02 02 00 00 00 01 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
# STATUS: the 1.100 s tick (capture_time 1100000000), ticks_skipped 0 (offset 24), all else zero
03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 ab 90 41 00 00 00 00 03 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
# STATE_CHANGE to CLEAN: WIN's EmptyBook, packet 4 message 0, exch_time its mDEntryTimestamp, cause 5 (EmptyBook); the book is empty
04 00 00 00 00 00 00 00 04 00 00 00 01 00 44 00
01 c2 eb 0b 00 00 00 00 40 59 7c 44 00 00 00 00
80 9b 8b 44 00 00 00 00 02 00 00 00 00 05 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

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
| `pcap_crosscheck.py` | `b3_pcapinfo` against the generator's independent pcap reader (`make pcap-crosscheck`) |
| `counters_compare.py` | `b3_decode --counters` against counters derived from manifest.json and intent.jsonl: sequencing, rptSeq, stale flags and points, every other counter, both feeds (`make counters-compare`) |
| `gen_instruments.py` | The instrument table (4.4) from the feed generator's config.toml |
| `expected_book.py` | The expected port 2 book.bin for one feed (4): port 1 from `intent_to_events.py`, stale state from `b3replay.py`, a small independent price-level book (uncapped, or capped with `--tracked-depth K`: a second implementation of 4.2 beside the C++), packet order and capture times from the pcap |
| `book_compare.py` | `b3_decode --book` of any book build against `expected_book.py` with the same N and K (its capped mode for a capped build) byte for byte on both feeds, the book key at every checkpoint for CLEAN instruments on sides not depth incomplete (the key's first N levels), and the book counters (`make book-compare`: uncapped, K = 16, K = 32, and N = 2 with K = 3 and uncapped) |
| `book_oracle.py` | A capped build against an uncapped build with the same N: identical records except K, the depth-incomplete flags and the levels of flagged sides; truncation and depth-incomplete statistics per (N, K) (`make book-oracle`) |
| `events_compare.py` | `b3_decode --events` against `intent_to_events.py`: both feeds byte-identical in full order, every build (`make events-compare`) |
| `b3feed.py` | One feed's answer-key messages in the feed's own packet order and capture times, from its pcap (3.7) |
| `make_goldens.py` | Port 1 and port 2 (capped, K = 16 and K = 32) goldens for the RTL (`make goldens`, below) |
| `decode_compare.py` | `b3_decode --decode` against intent.jsonl with `compare_decode.py --strict`, both feeds of every set, the schema set's features and B3's sample packet (`make decode-compare`) |
| `decode_crosscheck.py` | `b3_decode` counts and per-message listing against intent.jsonl and manifest.json, both feeds (`make decode-crosscheck`) |

```sh
python3 tools/intent_to_events.py data/small/intent.jsonl -o expected/small_events.bin
python3 tools/dump_events.py expected/small_events.bin --limit 20
python3 tools/compare_decode.py out/small/decode.jsonl data/small/intent.jsonl
python3 -m pytest tools/tests
make data && python3 -m pytest tools/tests   # data tests fail until make data has run
```

**Goldens** (`make data && make goldens`): for every set, `data/golden/<set>/` holds `expected_feedA.bin` and `expected_feedB.bin` (the converter, each in its feed's own order), `cpp_feedA.bin` and `cpp_feedB.bin` (the C++, release build), port 2 for the capped book at N = 5 with K = 16 and K = 32 (`expected_book_n5_k16_feedA.bin` from `expected_book.py`'s capped mode, `cpp_book_n5_k16_feedA.bin` from the k16 build, and likewise for Feed B and K = 32; the RTL golden is compared against the build with the same N and K), and `provenance.json` with the repo commit (and whether the tree was dirty), the SHA-256 and recorded commit of the set's generator `provenance.json`, the build parameters (port 1's, and under `books` each book's N, K, STATUS interval and MAX_INSTRUMENTS), and each file's SHA-256 and size. A set whose C++ files differ from the expected ones is not written. `data/` is gitignored; the goldens are rebuilt, not committed.

`intent_to_events.py` writes N, K and MAX_INSTRUMENTS into the header (defaults 5, 16, 64, matching `book_params.hpp`). For a golden file matched against another build, pass `--tracked-depth 32` (or `0` for uncapped) and so on.

`compare_decode.py` compares `instrument` and `news` only when the decode row has them (they need the instrument table and News reassembly). `--strict` requires them.
