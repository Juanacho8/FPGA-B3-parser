"""Helpers that build intent-shaped rows for tests."""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "feed_generator" / "tests" / "fixtures" / "b3_sample_packet" / "decoded.json"
TRADE_EVENT = Path(__file__).resolve().parent / "fixtures" / "trade_event_intent.jsonl"


def sample_packet_rows():
    """B3's sample packet (decoded.json) as intent rows."""
    d = json.loads(SAMPLE.read_text(encoding="utf-8"))
    h = d["packet_header"]
    rows = []
    for i, m in enumerate(d["messages"]):
        rows.append({"channel": h["channelNumber"], "sequenceVersion": h["sequenceVersion"],
                     "sequenceNumber": h["sequenceNumber"], "index": i,
                     "sendingTime": h["sendingTime"],
                     "templateId": m["message_header"]["templateId"],
                     "blockLength": m["message_header"]["blockLength"],
                     "version": m["message_header"]["version"],
                     "instrument": None, "fields": m["fields"], "null": []})
    return rows


def trade_event_rows():
    return [json.loads(line) for line in TRADE_EVENT.read_text(encoding="utf-8").splitlines()]


def row(template_id, fields, channel=68, seq=1, index=0, version=1, **extra):
    r = {"channel": channel, "sequenceVersion": version, "sequenceNumber": seq,
         "index": index, "sendingTime": 0, "templateId": template_id, "blockLength": 0,
         "version": 17, "instrument": None, "fields": fields, "null": []}
    r.update(extra)
    return r


def order(action=0, mei=0x80, side="0", **kw):
    return row(50, {"securityID": 7, "matchEventIndicator": mei, "mDUpdateAction": action,
                    "mDEntryType": side, "mDEntryPx": 100, "mDEntrySize": 5,
                    "enteringFirm": 9, "mDInsertTimestamp": 1, "secondaryOrderID": 42,
                    "rptSeq": 3, "transactTime": 1000, "mDEntryPrevSize": -(1 << 63)}, **kw)


def stats(mei=0x80, **kw):
    return row(56, {"securityID": 7, "matchEventIndicator": mei, "tradingSessionID": 1,
                    "tradeDate": 1, "tradeVolume": 1, "vwapPx": 1, "netChgPrevDay": 0,
                    "numberOfTrades": 1, "mDEntryTimestamp": 2000, "rptSeq": 4}, **kw)


def summary(**kw):
    return row(55, {"securityID": 7, "aggressorSide": 1, "lastPx": 100, "fillQty": 1,
                    "tradedHiddenQty": 0, "cxlQty": 0, "aggressorTime": 1, "rptSeq": 1,
                    "transactTime": 500}, **kw)


def heartbeat(**kw):
    kw.setdefault("seq", 0)
    return row(2, {"nextSeqNo": 5}, **kw)
