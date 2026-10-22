"""Encoder tests.

* B3's sample packet (Message Reference section 6) re-encodes byte for byte.
* Round trip through the independent decoder (tests/sbe_decoder.py, written
  from spec/LAYOUTS.md) for every template the generator emits.
* Null filling, zero padding, errors and the schema-evolution overrides.

Expected nulls and required/optional come from spec/LAYOUTS.md (via
tests/layouts_md.py), not from b3gen.layouts.
"""

import json
import struct
from pathlib import Path

import pytest

from b3gen import encode
from b3gen.encode import EncodeError
from layouts_md import MESSAGE_LAYOUTS
import sbe_decoder

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "b3_sample_packet"

# docs/FEED_GENERATOR_CONTEXT.md section 6.
EMITTED_TEMPLATES = sorted(sbe_decoder.LAYOUTS)

# Deprecated fields (spec/LAYOUTS.md section 7 Notes column). Always null.
DEPRECATED = {28: {"openCloseSettlFlag"}}

WRAPPERS = {
    1: encode.sequence_reset, 2: encode.sequence, 3: encode.security_status,
    5: encode.news, 9: encode.empty_book, 10: encode.security_group_phase,
    11: encode.channel_reset, 15: encode.opening_price, 17: encode.closing_price,
    21: encode.quantity_band, 22: encode.price_band, 24: encode.high_price,
    25: encode.low_price, 28: encode.settlement_price, 29: encode.open_interest,
    50: encode.order_mbo, 51: encode.delete_order_mbo, 52: encode.mass_delete_orders_mbo,
    53: encode.trade, 55: encode.execution_summary, 56: encode.execution_statistics,
    57: encode.trade_bust,
}


def load_fixture():
    hex_text = "".join(line for line in (FIXTURE / "packet.hex").read_text().splitlines()
                       if not line.startswith("#"))
    return bytes.fromhex(hex_text), json.loads((FIXTURE / "decoded.json").read_text())


def sample_value(code, i):
    """A distinct, non-zero, in-range value for a decoder struct code."""
    if code.endswith("s"):
        return {"1s": "1", "2s": "en", "3s": "ABC"}[code]
    return {
        "B": 1 + (37 * i) % 250,
        "H": 1000 + i,
        "I": 100000 + i,
        "Q": 2 ** 40 + i,
        "q": -(2 ** 40) - i,  # negative, to exercise signed encoding
    }[code]


def full_values(template_id):
    """Every non-deprecated field set to a distinct value."""
    _, fields = sbe_decoder.LAYOUTS[template_id]
    return {name: sample_value(code, i) for i, (_, code, name) in enumerate(fields)
            if name not in DEPRECATED.get(template_id, set())}


def optional_names(template_id):
    return set(MESSAGE_LAYOUTS[template_id]["optional_nulls"])


# ---------------------------------------------------------------------------
# B3 sample packet
# ---------------------------------------------------------------------------

def test_sample_packet_messages_reencode_byte_for_byte():
    packet, decoded = load_fixture()
    assert len(packet) == 168
    for msg in decoded["messages"]:
        start = msg["offset_in_packet"]
        expected = packet[start:start + msg["framing_header"]["messageLength"]]
        template_id = msg["message_header"]["templateId"]
        assert encode.encode(template_id, msg["fields"]) == expected, msg["template"]


def test_sample_packet_messages_fill_the_packet():
    # Packet header (16) + both messages = the whole 168 bytes. Step 3 adds
    # the packet header and rebuilds all of it from this fixture.
    packet, decoded = load_fixture()
    total = 16 + sum(m["framing_header"]["messageLength"] for m in decoded["messages"])
    assert total == len(packet)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template_id", EMITTED_TEMPLATES)
def test_round_trip_through_independent_decoder(template_id):
    values = full_values(template_id)
    if template_id == 5:
        values.update(headline="Leilão", text=b"x" * 300, uRLLink="http://www.b3.com.br")
    message = WRAPPERS[template_id](**values)

    header, decoded = sbe_decoder.decode(message)
    block_length = sbe_decoder.LAYOUTS[template_id][0]
    assert header == {
        "messageLength": len(message), "encodingType": 0xEB50,
        "blockLength": block_length, "templateId": template_id,
        "schemaId": 2, "version": 17,
    }
    for name, value in values.items():
        expected = value.encode("utf-8") if name in ("headline", "uRLLink") else value
        assert decoded[name] == expected, name
    for name in DEPRECATED.get(template_id, set()):
        assert decoded[name] == MESSAGE_LAYOUTS[template_id]["optional_nulls"][name]


def test_news_message_length_counts_var_data():
    headline, text, url = "Leilão".encode("utf-8"), b"x" * 300, b"http://www.b3.com.br"
    message = encode.news(**full_values(5), headline=headline, text=text, uRLLink=url)
    var_bytes = sum(2 + len(d) for d in (headline, text, url))
    header, _ = sbe_decoder.decode(message)
    assert header["messageLength"] == 12 + header["blockLength"] + var_bytes == len(message)


def test_sequence_reset_is_header_only():
    assert encode.sequence_reset() == struct.pack("<HHHHHH", 12, 0xEB50, 0, 1, 2, 17)


# ---------------------------------------------------------------------------
# Null filling and padding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template_id", EMITTED_TEMPLATES)
def test_optional_fields_left_out_get_their_null(template_id):
    optional = optional_names(template_id)
    required_only = {k: v for k, v in full_values(template_id).items() if k not in optional}
    _, decoded = sbe_decoder.decode(encode.encode(template_id, required_only))
    for name, null in MESSAGE_LAYOUTS[template_id]["optional_nulls"].items():
        # A char field's null 0 is all zero bytes, which decodes as "".
        expected = "" if isinstance(decoded[name], str) and null == 0 else null
        assert decoded[name] == expected, name


@pytest.mark.parametrize("template_id", EMITTED_TEMPLATES)
def test_padding_bytes_are_zero(template_id):
    block_length, fields = sbe_decoder.LAYOUTS[template_id]
    values = {}
    for _, code, name in fields:
        if name in DEPRECATED.get(template_id, set()):
            continue
        if code.endswith("s"):
            values[name] = "z" * int(code[:-1])
        elif code.islower():
            values[name] = -1  # all bits set
        else:
            values[name] = 2 ** (8 * struct.calcsize(code)) - 1
    body = encode.encode(template_id, values)[12:]
    covered = set()
    for offset, code, _ in fields:
        covered.update(range(offset, offset + struct.calcsize(code)))
    padding = [i for i in range(block_length) if i not in covered]
    assert all(body[i] == 0 for i in padding), padding


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def order_values():
    return full_values(50)


def test_missing_required_field_raises():
    values = order_values()
    del values["securityID"]
    with pytest.raises(EncodeError, match="securityID: required field missing"):
        encode.order_mbo(**values)


def test_unknown_field_raises():
    with pytest.raises(EncodeError, match="unknown field"):
        encode.order_mbo(**order_values(), price=1)


def test_constant_field_raises():
    # DeleteOrder_MBO mDUpdateAction is the constant DELETE.
    with pytest.raises(EncodeError, match="constant"):
        encode.delete_order_mbo(**full_values(51), mDUpdateAction=2)


def test_deprecated_field_raises():
    with pytest.raises(EncodeError, match="deprecated"):
        encode.settlement_price(**full_values(28), openCloseSettlFlag=0)


@pytest.mark.parametrize("name, value", [
    ("matchEventIndicator", 256),      # uint8 too big
    ("matchEventIndicator", -1),       # uint8 negative
    ("enteringFirm", 2 ** 32),         # uint32 too big
    ("mDEntryPx", 2 ** 63),            # int64 too big
    ("mDEntryPx", -(2 ** 63) - 1),     # int64 too small
    ("securityID", 2 ** 64),           # uint64 too big
])
def test_out_of_range_raises(name, value):
    with pytest.raises(EncodeError, match="out of range"):
        encode.order_mbo(**{**order_values(), name: value})


@pytest.mark.parametrize("value", [1234.5678, True, "12345678", None])
def test_non_int_for_int_field_raises(value):
    with pytest.raises(EncodeError, match="expected int"):
        encode.order_mbo(**{**order_values(), "mDEntryPx": value})


def test_char_too_long_raises():
    with pytest.raises(EncodeError, match="longer than"):
        encode.order_mbo(**{**order_values(), "mDEntryType": "01"})


def test_unknown_template_raises():
    with pytest.raises(EncodeError, match="unknown template"):
        encode.encode(99, {})


def test_message_larger_than_a_packet_raises():
    with pytest.raises(EncodeError, match="does not fit"):
        encode.news(**full_values(5), text=b"x" * 1400)


# ---------------------------------------------------------------------------
# Overrides (schema evolution scenario)
# ---------------------------------------------------------------------------

def test_block_length_override_appends_zero_bytes():
    values = order_values()
    normal = encode.order_mbo(**values)
    longer = encode.encode(50, values, block_length=72 + 8)
    assert len(longer) == len(normal) + 8
    header, decoded = sbe_decoder.decode(longer)
    assert header["blockLength"] == 80 and header["messageLength"] == 12 + 80
    assert longer[12 + 72:] == bytes(8)
    assert longer[12:12 + 72] == normal[12:]
    assert {k: decoded[k] for k in values} == values


def test_block_length_override_below_schema_raises():
    with pytest.raises(EncodeError, match="block_length"):
        encode.encode(50, order_values(), block_length=71)


def test_block_length_override_shifts_var_data():
    values = {**full_values(5), "text": b"hello"}
    header, decoded = sbe_decoder.decode(encode.encode(5, values, block_length=36 + 4))
    assert header["blockLength"] == 40 and decoded["text"] == b"hello"


def test_version_override():
    header, _ = sbe_decoder.decode(encode.encode(50, order_values(), version=18))
    assert header["version"] == 18


# ---------------------------------------------------------------------------
# Groups (not emitted in v1, but encode() supports them)
# ---------------------------------------------------------------------------

def test_group_dimension_header_and_entries():
    entry = {"mDEntryPx": 100, "mDEntrySize": 5, "mDInsertTimestamp": 1,
             "secondaryOrderID": 7, "mDEntryType": "0", "matchEventIndicator": 0}
    message = encode.encode(71, {"securityID": 1, "noMDEntries": [entry, entry]})
    # Root block 8, then GroupSizeEncoding: blockLength 42, numInGroup 2.
    assert struct.unpack_from("<HB", message, 12 + 8) == (42, 2)
    assert len(message) == 12 + 8 + 3 + 2 * 42
    first = 12 + 8 + 3
    assert struct.unpack_from("<qq", message, first) == (100, 5)
    assert message[first + 40:first + 41] == b"0"
