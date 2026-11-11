# Unit test for pkg_types: the SV record and tb/lib/record.py agree bit for bit.
#
# 1. Width: $bits(decoded_msg_t) == 644
# 2. Message Reference section 6 sample (Order_MBO_50): known values land
#    in the right bits and bytes
# 3. Random values: SV packing == Python pack(), 200 times

import random

import cocotb
from cocotb.triggers import Timer

import record


# Message Reference v2.3.1 section 6 sample packet, message 1 (Order_MBO_50).
# Values from feed_generator/tests/fixtures/b3_sample_packet/decoded.json.
SAMPLE_ORDER = {
    "channel": 55,
    "seq_version": 1,
    "seq_num": 987654321,
    "msg_index": 0,
    "template_id": 50,
    "schema_version": 17,
    "absent": 0,
    "security_id": 100000109220,
    "match_event_ind": 0x80,
    "md_update_action": 1,
    "md_entry_type": ord("1"),
    "md_entry_px": 12345678,
    "md_entry_size": 10000,
    "entering_firm": 0,
    "md_insert_ts": 0,
    "secondary_order_id": 0,
    "rpt_seq": 0,
    "transact_time": 1579546260000000000,
    "md_entry_prev_size": 0,
}


async def drive(dut, values):
    for name, _ in record.FIELDS:
        getattr(dut, name).value = values[name]
    await Timer(1, unit="ns")
    return dut.rec.value.to_unsigned()


@cocotb.test()
async def test_width(dut):
    await Timer(1, unit="ns")
    assert dut.rec_bits.value.to_unsigned() == record.RECORD_BITS == 644


@cocotb.test()
async def test_sample_order(dut):
    # ---- 1. SV packing equals Python packing ----
    rec = await drive(dut, SAMPLE_ORDER)
    assert rec == record.pack(SAMPLE_ORDER)

    # ---- 2. Spot-check positions from the pkg_types bit table ----
    assert (rec >> 636) & 0xFF == 55                              # channel [643:636]
    assert (rec >> 472) & (2**64 - 1) == 0x17487892A4             # security_id [535:472]
    assert (rec >> 448) & 0xFF == 0x31                            # md_entry_type [455:448]
    assert (rec >> 64) & (2**64 - 1) == 1579546260000000000       # transact_time [127:64]

    # ---- 3. Byte form: transact_time is bytes 8..15, little-endian ----
    data = record.to_bytes(rec)
    assert len(data) == 81
    assert data[8:16] == bytes.fromhex("00c89865f4aceb15")        # same bytes as on the wire


@cocotb.test()
async def test_random(dut):
    rng = random.Random(1)
    for _ in range(200):
        values = {name: rng.getrandbits(width) for name, width in record.FIELDS}
        rec = await drive(dut, values)
        assert rec == record.pack(values), f"mismatch for {values}"
