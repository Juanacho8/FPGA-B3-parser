# RTL parser design decisions

Decisions for `rtl_parser/`. B3's wire format is in `spec/LAYOUTS.md`; this
file records our choices on top of it. Resolves open item A of
`PROJECT_CONTEXT.md` (parser output contract).

## 1. Interfaces

* **Byte streams** between modules are 64-bit AXI-Stream: `tdata[63:0]`,
  `tkeep[7:0]`, `tvalid`, `tready`, `tlast`. Stream byte 0 is `tdata[7:0]`.
  B3 is little-endian, so fields are plain bit slices.
* **Decoded records** use valid/ready with a packed struct (`decoded_msg_t`
  in `rtl/pkg/pkg_types.sv`), not 64-bit AXI-Stream.

## 2. Decoded-message record (`decoded_msg_t`, 644 bits)

One fixed-width superset record for templates 50, 51 and 52. In a packed
struct the first member is the highest bits.

| Bits | Field | Width | Order_MBO_50 | DeleteOrder_MBO_51 | MassDeleteOrders_MBO_52 | absent bit |
|---|---|---|---|---|---|---|
| [643:636] | channel | 8 | packet header 0 | same | same | |
| [635:620] | seq_version | 16 | packet header 2 | same | same | |
| [619:588] | seq_num | 32 | packet header 4 | same | same | |
| [587:580] | msg_index | 8 | position in packet (skipped messages included) | same | same | |
| [579:564] | template_id | 16 | message header 6 | same | same | |
| [563:548] | schema_version | 16 | message header 10 | same | same | |
| [547:536] | absent | 12 | | | | |
| [535:472] | security_id | 64 | body 0 / 8 | 0 / 8 | 0 / 8 | 0 |
| [471:464] | match_event_ind | 8 | 8 / 1 | 8 / 1 | 8 / 1 | 1 |
| [463:456] | md_update_action | 8 | 9 / 1 | constant (not stored) | 9 / 1 | 2 |
| [455:448] | md_entry_type | 8 | 10 / 1 | 10 / 1 | 10 / 1 | 3 |
| [447:384] | md_entry_px | 64 | 12 / 8 | 44 / 8 | | 4 |
| [383:320] | md_entry_size | 64 | 20 / 8 | 16 / 8 | | 5 |
| [319:288] | entering_firm | 32 | 32 / 4 | | | 6 |
| [287:224] | md_insert_ts | 64 | 36 / 8 | | | 7 |
| [223:160] | secondary_order_id | 64 | 44 / 8 | 24 / 8 | | 8 |
| [159:128] | rpt_seq | 32 | 52 / 4 | 40 / 4 | 24 / 4 | 9 |
| [127:64] | transact_time | 64 | 56 / 8 | 32 / 8 | 16 / 8 | 10 |
| [63:0] | md_entry_prev_size | 64 | 64 / 8 | | | 11 |

Offsets are body offsets / sizes from `spec/LAYOUTS.md` section 7.

Field rules:

* Values are raw wire bits. Nulls keep their wire encoding; there is no null flag.
* **absent = 1, value = 0** when a field is not on the wire for this message:
  * the template doesn't carry it (e.g. 51 has no enteringFirm; its
    mDUpdateAction is a schema constant), or
  * the header's blockLength cuts it off (offset + size > blockLength), as
    for older schema versions (Guidelines 5.9-5.10).
* Constant fields are not stored; templateId implies them.
* Comparison form for tests: the 644-bit value as 81 bytes, little-endian.
* The test reference built from `intent.jsonl` applies the same absent rule:
  every record field that is not in the row's `fields` is absent. Its
  `absent` list alone only covers fields missing because of an older version.

## 3. Schema versions

Trust the header (Guidelines 5.9-5.10): decode v17 layouts, mark fields
beyond blockLength as absent, skip bytes past the known fields, skip unknown
templates by messageLength.

**Known limitation.** In templates 51 and 52 a field added in version 15
sits before an older field (schema XML):

```xml
<!-- DeleteOrder_MBO_51 -->
<field name="transactTime" id="60" type="UTCTimestampNanos" sinceVersion="15" />
<field name="rptSeq" id="83" type="RptSeq" />
<!-- MassDeleteOrders_MBO_52 -->
<field name="transactTime" id="60" type="UTCTimestampNanos" sinceVersion="15"  offset="16"/>
<field name="rptSeq" id="83" type="RptSeq" />
```

So pre-v15 51 and 52 messages used a different layout, and "absent if
offset + size > blockLength" cannot describe them. We decode them with v17
offsets anyway, with no extra logic. The generator never sends them; it only
sends older versions of 50, where both rules agree.

## 4. Malformed packets (project policy, not B3)

| Condition | Action |
|---|---|
| Datagram shorter than the 16-byte packet header | Drop it |
| messageLength < 12 | Drop the rest of the packet |
| encodingType != 0xEB50 | Drop the rest of the packet |
| blockLength > messageLength - 12 | Drop the rest of the packet |
| Packet ends before the message's messageLength | The message is marked bad; no record |
| Packet ends inside the first 6 bytes of a message header | Drop it (nothing to check) |

Complete messages before the fault are kept. The blockLength rule is not
covered by the generator's malformed scenario; the msg_splitter unit test
covers it with a hand-made packet.

## 5. msg_splitter: realignment

msg_splitter shifts every message so its byte 0 sits in lane 0 of its first
output beat. The decoder then finds each field at a fixed beat and lane.

Cost:

* **+1 clock per message** (6.4 ns at 156.25 MHz): a shifted beat needs bytes
  from the next input beat.
* **No throughput loss.** A message whose length is not a multiple of 8 ends
  in a partial beat. Per packet there are about 10 beats of slack (the 2-beat
  B3 packet header, which produces no output, plus about 8 beats of
  Ethernet/IP/UDP overhead and inter-frame gap), against at most 8 extra
  beats (16 Order_MBO of 84 bytes x 0.5). The real cost is up to about 8 beats
  (~51 ns) of lag accumulating on the last Orders of a dense packet, recovered
  before the next packet.
* **Hardware needs a ~16-beat buffer** in front of msg_splitter, because the
  MAC cannot be backpressured. Not needed in Phase 1 simulation, where the
  source waits on tready.
* Revisit in Phase 5: a faster core clock, or pass-through (keep the original
  lanes and give the decoder the start lane).

## 6. Record timing

A decoded record's valid rises one clock after the message's last byte
according to messageLength, and only if msg_splitter marks the message good.
For schema v17 that is the same clock as the last needed byte. Messages with
a larger blockLength (e.g. v18) wait for their extra bytes.

## 7. Planned additions

* **Phase 3 (ID lookup):** an early sideband from sbe_decoder with securityID
  and templateId, valid as soon as message bytes 12-19 arrive (securityID is
  at body offset 0 in all three templates), so ID lookup starts while the
  rest of the message streams in.
