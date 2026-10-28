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


def write_pcap(path, rows, times=None):
    """A pcap whose datagrams carry the rows' packet headers, one datagram per
    packet (a row with index 0 starts one), at times (ns) or 1 us apart. The
    answer keys read only the headers and times (tools/b3feed.py)."""
    import struct
    from test_pcap_crosscheck import frame, pcap
    starts = [r for r in rows if r["index"] == 0]
    times = times or [1_000_000_000 + 1000 * i for i in range(len(starts))]
    frames = []
    for r, t in zip(starts, times):
        payload = struct.pack("<BBHIQ", r["channel"], 0, r["sequenceVersion"], r["sequenceNumber"],
                              r.get("sendingTime", 0)) + bytes(12)
        frames.append((frame(payload), t))
    data = bytearray(pcap([(fr, 0) for fr, _ in frames]))
    off = 24
    for fr, t in frames:            # the record times
        struct.pack_into("<II", data, off, t // 1_000_000_000, t % 1_000_000_000)
        off += 16 + len(fr)
    Path(path).write_bytes(bytes(data))
