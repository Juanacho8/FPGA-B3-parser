"""Independent test decoder for the templates the generator emits.

Offsets and types are copied by hand from spec/LAYOUTS.md section 7. This
module never imports b3gen.layouts: it is a second, independent reading of
the wire format, so an error in the generator or encoder cannot hide itself.
Only used by tests, never to produce answer keys.

Struct codes: B/H/I/Q unsigned 1/2/4/8 bytes, b/h/i/q signed, Ns = char[N].
"""

import struct

# template id -> (blockLength, [(offset, struct code, field name), ...])
LAYOUTS = {
    1: (0, []),  # SequenceReset: constants only
    2: (4, [(0, "I", "nextSeqNo")]),
    3: (36, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"),
        (9, "B", "tradingSessionID"), (10, "B", "securityTradingStatus"),
        (11, "B", "securityTradingEvent"), (12, "H", "tradeDate"),
        (16, "Q", "tradSesOpenTime"), (24, "Q", "transactTime"), (32, "I", "rptSeq"),
    ]),
    5: (36, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (9, "B", "newsSource"),
        (10, "2s", "languageCode"), (12, "H", "partCount"), (14, "H", "partNumber"),
        (16, "Q", "newsID"), (24, "Q", "origTime"), (32, "I", "totalTextLength"),
    ]),
    9: (20, [(0, "Q", "securityID"), (8, "B", "matchEventIndicator"),
             (12, "Q", "mDEntryTimestamp")]),
    10: (32, [
        (0, "3s", "securityGroup"), (8, "B", "matchEventIndicator"),
        (9, "B", "tradingSessionID"), (10, "B", "tradingSessionSubID"),
        (11, "B", "securityTradingEvent"), (12, "H", "tradeDate"),
        (16, "Q", "tradSesOpenTime"), (24, "Q", "transactTime"),
    ]),
    11: (12, [(0, "B", "matchEventIndicator"), (4, "Q", "mDEntryTimestamp")]),
    15: (44, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (9, "B", "mDUpdateAction"),
        (10, "B", "openCloseSettlFlag"), (12, "q", "mDEntryPx"), (20, "q", "netChgPrevDay"),
        (28, "H", "tradeDate"), (30, "Q", "mDEntryTimestamp"), (38, "I", "rptSeq"),
    ]),
    17: (36, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"),
        (9, "B", "openCloseSettlFlag"), (12, "q", "mDEntryPx"), (20, "H", "lastTradeDate"),
        (22, "H", "tradeDate"), (24, "Q", "mDEntryTimestamp"), (32, "I", "rptSeq"),
    ]),
    21: (40, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"),
        (12, "q", "avgDailyTradedQty"), (20, "q", "maxTradeVol"),
        (28, "Q", "mDEntryTimestamp"), (36, "I", "rptSeq"),
    ]),
    22: (48, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (9, "B", "priceBandType"),
        (10, "B", "priceLimitType"), (11, "B", "priceBandMidpointPriceType"),
        (12, "q", "lowLimitPrice"), (20, "q", "highLimitPrice"),
        (28, "q", "tradingReferencePrice"), (36, "Q", "mDEntryTimestamp"), (44, "I", "rptSeq"),
    ]),
    24: (32, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (9, "B", "mDUpdateAction"),
        (10, "H", "tradeDate"), (12, "q", "mDEntryPx"), (20, "Q", "mDEntryTimestamp"),
        (28, "I", "rptSeq"),
    ]),
    25: (32, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (9, "B", "mDUpdateAction"),
        (10, "H", "tradeDate"), (12, "q", "mDEntryPx"), (20, "Q", "mDEntryTimestamp"),
        (28, "I", "rptSeq"),
    ]),
    28: (40, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (10, "H", "tradeDate"),
        (12, "q", "mDEntryPx"), (20, "Q", "mDEntryTimestamp"),
        (28, "B", "openCloseSettlFlag"), (29, "B", "priceType"), (30, "B", "settlPriceType"),
        (31, "I", "rptSeq"), (35, "H", "settlDate"),
    ]),
    29: (32, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (10, "H", "tradeDate"),
        (12, "q", "mDEntrySize"), (20, "Q", "mDEntryTimestamp"), (28, "I", "rptSeq"),
    ]),
    50: (72, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (9, "B", "mDUpdateAction"),
        (10, "1s", "mDEntryType"), (12, "q", "mDEntryPx"), (20, "q", "mDEntrySize"),
        (32, "I", "enteringFirm"), (36, "Q", "mDInsertTimestamp"),
        (44, "Q", "secondaryOrderID"), (52, "I", "rptSeq"), (56, "Q", "transactTime"),
        (64, "q", "mDEntryPrevSize"),
    ]),
    51: (52, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (10, "1s", "mDEntryType"),
        (16, "q", "mDEntrySize"), (24, "Q", "secondaryOrderID"), (32, "Q", "transactTime"),
        (40, "I", "rptSeq"), (44, "q", "mDEntryPx"),
    ]),
    52: (28, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (9, "B", "mDUpdateAction"),
        (10, "1s", "mDEntryType"), (16, "Q", "transactTime"), (24, "I", "rptSeq"),
    ]),
    53: (56, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (9, "B", "tradingSessionID"),
        (10, "H", "tradeCondition"), (12, "q", "mDEntryPx"), (20, "q", "mDEntrySize"),
        (28, "I", "tradeID"), (32, "I", "mDEntryBuyer"), (36, "I", "mDEntrySeller"),
        (40, "H", "tradeDate"), (42, "B", "trdSubType"), (44, "Q", "transactTime"),
        (52, "I", "rptSeq"),
    ]),
    55: (64, [
        (0, "Q", "securityID"), (10, "B", "aggressorSide"), (12, "q", "lastPx"),
        (20, "q", "fillQty"), (28, "q", "tradedHiddenQty"), (36, "q", "cxlQty"),
        (44, "Q", "aggressorTime"), (52, "I", "rptSeq"), (56, "Q", "transactTime"),
    ]),
    56: (52, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (9, "B", "tradingSessionID"),
        (10, "H", "tradeDate"), (12, "q", "tradeVolume"), (20, "q", "vwapPx"),
        (28, "q", "netChgPrevDay"), (36, "I", "numberOfTrades"),
        (40, "Q", "mDEntryTimestamp"), (48, "I", "rptSeq"),
    ]),
    57: (48, [
        (0, "Q", "securityID"), (8, "B", "matchEventIndicator"), (9, "B", "tradingSessionID"),
        (12, "q", "mDEntryPx"), (20, "q", "mDEntrySize"), (28, "I", "tradeID"),
        (32, "H", "tradeDate"), (36, "Q", "transactTime"), (44, "I", "rptSeq"),
    ]),
}

# template id -> var data names in order, each with a uint16 length prefix
# (News: headline, text, uRLLink are VarString).
VAR_DATA = {5: ["headline", "text", "uRLLink"]}


def decode(message):
    """Decode one message. Returns (header dict, fields dict).

    Chars come back as str with trailing NULs stripped. Var data comes back
    as bytes."""
    message_length, encoding_type, block_length, template_id, schema_id, version = \
        struct.unpack_from("<HHHHHH", message, 0)
    header = {
        "messageLength": message_length, "encodingType": encoding_type,
        "blockLength": block_length, "templateId": template_id,
        "schemaId": schema_id, "version": version,
    }
    if template_id not in LAYOUTS:
        return header, None  # unknown template: skipped by messageLength
    _, fields_layout = LAYOUTS[template_id]
    body = 12
    fields = {}
    for offset, code, name in fields_layout:
        (value,) = struct.unpack_from("<" + code, message, body + offset)
        if isinstance(value, bytes):
            value = value.rstrip(b"\x00").decode("ascii")
        fields[name] = value
    # Trust the header's blockLength to find the end of the root block.
    position = body + block_length
    for name in VAR_DATA.get(template_id, []):
        (length,) = struct.unpack_from("<H", message, position)
        fields[name] = bytes(message[position + 2:position + 2 + length])
        position += 2 + length
    assert position == message_length, (position, message_length)
    return header, fields
