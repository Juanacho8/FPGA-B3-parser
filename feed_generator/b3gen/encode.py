"""Support: message encoder, driven entirely by layouts.

encode(template_id, values) turns a dict of field values into one complete
SBE message: framing header (messageLength, encodingType), SBE message
header (blockLength, templateId, schemaId, version), root block, then groups
and var data. Little-endian throughout. Padding bytes are zero.

Value rules:
* Optional fields left out get their null. Missing required fields raise.
* Unknown names raise. So do constant fields (they take 0 bytes) and
  deprecated fields: deprecated fields are always written as null.
* Integers are range-checked against their primitive. bool and float are
  rejected, so decimals are passed as their integer mantissa
  (Price 1234.5678 -> 12345678).
* char[N] takes ASCII str or bytes, right-padded with 0x00.
* Multi-member composites (MaturityMonthYear) take a dict by member name.
* Groups take a list of dicts. Var data takes bytes or str (UTF-8).

Overrides for the schema evolution scenario: block_length larger than the
schema (extra zero bytes after the known fields) and version.

The thin wrappers at the bottom name the templates the generator emits
(docs/FEED_GENERATOR_CONTEXT.md section 6).

Must not: decide field values (that is the publisher's job) or group
messages into packets (packet header belongs to packet.py).
See spec/LAYOUTS.md.
"""

import struct

from b3gen import layouts

# Framing header encodingType: SBE 1.0 little-endian (bytes 50 EB on the
# wire). Defined by B3's documents, not by the XML (spec/LAYOUTS.md section 4).
ENCODING_TYPE = 0xEB50

# A message must fit in one packet: at most 1400 bytes minus the packet header.
MAX_PACKET_SIZE = 1400
PACKET_HEADER_SIZE = sum(m.size for m in layouts.PACKET_HEADER)
MAX_MESSAGE_SIZE = MAX_PACKET_SIZE - PACKET_HEADER_SIZE

FRAMING_HEADER_SIZE = sum(m.size for m in layouts.FRAMING_HEADER)
SBE_HEADER_SIZE = sum(m.size for m in layouts.MESSAGE_HEADER)
HEADER_SIZE = FRAMING_HEADER_SIZE + SBE_HEADER_SIZE

STRUCT_CODES = {
    "int8": "b", "uint8": "B",
    "int16": "h", "uint16": "H",
    "int32": "i", "uint32": "I",
    "int64": "q", "uint64": "Q",
}


class EncodeError(ValueError):
    """A value or template cannot be encoded."""


# ---------------------------------------------------------------------------
# Primitive writers
# ---------------------------------------------------------------------------

def int_range(primitive):
    bits = struct.calcsize(STRUCT_CODES[primitive]) * 8
    if primitive.startswith("u"):
        return 0, 2 ** bits - 1
    return -(2 ** (bits - 1)), 2 ** (bits - 1) - 1


def write_int(buf, offset, primitive, value, where):
    # bool is a subclass of int; reject it along with float and the rest.
    if type(value) is not int:
        raise EncodeError(f"{where}: expected int, got {type(value).__name__} {value!r}")
    low, high = int_range(primitive)
    if not low <= value <= high:
        raise EncodeError(f"{where}: {value} out of range for {primitive} [{low}, {high}]")
    struct.pack_into("<" + STRUCT_CODES[primitive], buf, offset, value)


def write_chars(buf, offset, size, value, where):
    """char or char[N]: ASCII, right-padded with 0x00. The int 0 (the
    schema null for optional char fields) writes all zeros."""
    if value == 0 and type(value) is int:
        return  # buffer is already zero
    if isinstance(value, str):
        try:
            value = value.encode("ascii")
        except UnicodeEncodeError:
            raise EncodeError(f"{where}: {value!r} is not ASCII") from None
    if not isinstance(value, bytes):
        raise EncodeError(f"{where}: expected str or bytes, got {type(value).__name__}")
    if len(value) > size:
        raise EncodeError(f"{where}: {value!r} longer than {size} bytes")
    buf[offset:offset + len(value)] = value


def write_members(buf, base, members, values, where, use_nulls):
    """Write header or composite members. Missing members get their null
    when use_nulls is set (and they have one), otherwise raise."""
    unknown = set(values) - {m.name for m in members}
    if unknown:
        raise EncodeError(f"{where}: unknown member(s) {sorted(unknown)}")
    for m in members:
        mwhere = f"{where}.{m.name}"
        if m.name in values:
            value = values[m.name]
        elif use_nulls and m.null is not None:
            value = m.null
        else:
            raise EncodeError(f"{mwhere}: required member missing")
        write_int(buf, base + m.offset, m.primitive, value, mwhere)


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

def check_names(values, fields, constants, extra, where):
    """Reject constants, deprecated fields and anything unknown."""
    constant_names = {c.name for c in constants}
    field_names = {f.name for f in fields}
    for name in values:
        if name in constant_names:
            raise EncodeError(f"{where}.{name}: constant field, not encoded")
        if name not in field_names and name not in extra:
            raise EncodeError(f"{where}.{name}: unknown field")
    for f in fields:
        if f.deprecated is not None and f.name in values:
            raise EncodeError(f"{where}.{f.name}: deprecated in schema {f.deprecated}; "
                              f"always written as null")


def write_block(buf, base, fields, values, where):
    """Write the fixed fields of a root block or group entry at buf[base:]."""
    for f in fields:
        fwhere = f"{where}.{f.name}"
        if f.deprecated is not None or f.name not in values:
            # Deprecated fields still occupy their bytes and are written as
            # null. Optional fields left out get their null.
            if f.presence != "optional":
                raise EncodeError(f"{fwhere}: required field missing")
            if f.primitive == "composite":
                write_members(buf, base + f.offset, f.members, {}, fwhere, use_nulls=True)
            elif f.primitive == "char":
                write_chars(buf, base + f.offset, f.size, f.null, fwhere)
            else:
                write_int(buf, base + f.offset, f.primitive, f.null, fwhere)
            continue
        value = values[f.name]
        if f.primitive == "composite":
            if not isinstance(value, dict):
                raise EncodeError(f"{fwhere}: expected dict of members")
            write_members(buf, base + f.offset, f.members, value, fwhere, use_nulls=True)
        elif f.primitive == "char":
            write_chars(buf, base + f.offset, f.size, value, fwhere)
        else:
            write_int(buf, base + f.offset, f.primitive, value, fwhere)


def encode_group(group, entries, where):
    """Dimension header (blockLength, numInGroup), then the entries."""
    if not isinstance(entries, (list, tuple)):
        raise EncodeError(f"{where}: expected a list of entries")
    dimension = bytearray(group.dimension_size)
    write_members(dimension, 0, group.dimension,
                  {"blockLength": group.block_length, "numInGroup": len(entries)},
                  where, use_nulls=False)
    body = bytearray(group.block_length * len(entries))
    for i, entry in enumerate(entries):
        ewhere = f"{where}[{i}]"
        check_names(entry, group.fields, group.constants, set(), ewhere)
        write_block(body, i * group.block_length, group.fields, entry, ewhere)
    return bytes(dimension) + bytes(body)


def encode_var_data(data, value, where):
    """Length prefix (prefix_size bytes), then the bytes."""
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, bytes):
        raise EncodeError(f"{where}: expected bytes or str")
    prefix = bytearray(data.prefix_size)
    write_int(prefix, 0, data.prefix_primitive, len(value), f"{where} length")
    return bytes(prefix) + value


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------

def encode(template_id, values, *, block_length=None, version=None):
    """Encode one message (framing header + SBE header + body) to bytes.

    block_length: override, must be >= the schema blockLength. The extra
    bytes are zero. version: override for the header's schema version."""
    template = layouts.TEMPLATES.get(template_id)
    if template is None:
        raise EncodeError(f"unknown template id {template_id}")
    where = template.name
    values = dict(values)

    extra = {g.name for g in template.groups} | {d.name for d in template.var_data}
    check_names(values, template.fields, template.constants, extra, where)

    if block_length is None:
        block_length = template.block_length
    if block_length < template.block_length:
        raise EncodeError(f"{where}: block_length {block_length} < schema "
                          f"{template.block_length}")

    root = bytearray(block_length)  # zero-filled: padding and the extension tail
    write_block(root, 0, template.fields, values, where)

    tail = b""
    for g in template.groups:
        tail += encode_group(g, values.get(g.name, []), f"{where}.{g.name}")
    for d in template.var_data:
        tail += encode_var_data(d, values.get(d.name, b""), f"{where}.{d.name}")

    message_length = HEADER_SIZE + block_length + len(tail)
    if message_length > MAX_MESSAGE_SIZE:
        raise EncodeError(f"{where}: {message_length} bytes does not fit in one packet "
                          f"(max {MAX_MESSAGE_SIZE})")

    header = bytearray(HEADER_SIZE)
    write_members(header, 0, layouts.FRAMING_HEADER,
                  {"messageLength": message_length, "encodingType": ENCODING_TYPE},
                  where + " framing header", use_nulls=False)
    write_members(header, FRAMING_HEADER_SIZE, layouts.MESSAGE_HEADER,
                  {"blockLength": block_length,
                   "templateId": template.id,
                   "schemaId": layouts.SCHEMA_ID,
                   "version": layouts.SCHEMA_VERSION if version is None else version},
                  where + " message header", use_nulls=False)
    return bytes(header) + bytes(root) + tail


# ---------------------------------------------------------------------------
# Named wrappers: the templates the generator emits
# ---------------------------------------------------------------------------

def sequence_reset(**fields):
    return encode(1, fields)


def sequence(**fields):
    return encode(2, fields)


def security_status(**fields):
    return encode(3, fields)


def news(**fields):
    return encode(5, fields)


def empty_book(**fields):
    return encode(9, fields)


def security_group_phase(**fields):
    return encode(10, fields)


def channel_reset(**fields):
    return encode(11, fields)


def opening_price(**fields):
    return encode(15, fields)


def closing_price(**fields):
    return encode(17, fields)


def quantity_band(**fields):
    return encode(21, fields)


def price_band(**fields):
    return encode(22, fields)


def high_price(**fields):
    return encode(24, fields)


def low_price(**fields):
    return encode(25, fields)


def settlement_price(**fields):
    return encode(28, fields)


def open_interest(**fields):
    return encode(29, fields)


def order_mbo(**fields):
    return encode(50, fields)


def delete_order_mbo(**fields):
    return encode(51, fields)


def mass_delete_orders_mbo(**fields):
    return encode(52, fields)


def trade(**fields):
    return encode(53, fields)


def execution_summary(**fields):
    return encode(55, fields)


def execution_statistics(**fields):
    return encode(56, fields)


def trade_bust(**fields):
    return encode(57, fields)
