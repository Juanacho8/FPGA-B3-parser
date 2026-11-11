# Python mirror of pkg_types::decoded_msg_t.
#
# 1. FIELDS lists the record MSB first, exactly like the packed struct.
# 2. pack(values) builds the 644-bit integer the RTL outputs.
# 3. to_bytes(value) gives the 81-byte form compared byte for byte.

# ---- 1. Layout, MSB first (must match rtl/pkg/pkg_types.sv) ----
FIELDS = [
    # context
    ("channel", 8),
    ("seq_version", 16),
    ("seq_num", 32),
    ("msg_index", 8),
    ("template_id", 16),
    ("schema_version", 16),
    # absent mask
    ("absent", 12),
    # fields, Order_MBO_50 wire order
    ("security_id", 64),
    ("match_event_ind", 8),
    ("md_update_action", 8),
    ("md_entry_type", 8),
    ("md_entry_px", 64),
    ("md_entry_size", 64),
    ("entering_firm", 32),
    ("md_insert_ts", 64),
    ("secondary_order_id", 64),
    ("rpt_seq", 32),
    ("transact_time", 64),
    ("md_entry_prev_size", 64),
]

RECORD_BITS = sum(width for _, width in FIELDS)   # 644
RECORD_BYTES = (RECORD_BITS + 7) // 8             # 81


# ---- 2. Pack: shift each field in, MSB field first ----
def pack(values):
    record = 0
    for name, width in FIELDS:
        mask = (1 << width) - 1
        field = values.get(name, 0) & mask   # negative ints -> two's complement
        record = (record << width) | field
    return record


# ---- 3. Byte form for the diff: little-endian, byte 0 = lowest bits ----
def to_bytes(record):
    return record.to_bytes(RECORD_BYTES, "little")
