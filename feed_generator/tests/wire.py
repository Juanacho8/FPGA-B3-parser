"""Independent wire helpers shared by the end-to-end tests (no b3gen)."""

import struct
from pathlib import Path

import pcap_reader

HEADER = struct.Struct("<BBHIQ")


def read_packets(pcap_path):
    """[{"time", "channel", "version", "seq", "sending", "payload", "messages"}]
    in file order, decoded from the pcap alone."""
    _, records = pcap_reader.read_pcap(Path(pcap_path).read_bytes())
    out = []
    for rec in records:
        payload = rec["payload"]
        channel, _, version, seq, sending = HEADER.unpack_from(payload, 0)
        messages, pos = [], HEADER.size
        while pos < len(payload):
            (length,) = struct.unpack_from("<H", payload, pos)
            messages.append(payload[pos:pos + length])
            pos += length
        assert pos == len(payload)
        out.append({"time": rec["time_ns"], "channel": channel, "version": version,
                    "seq": seq, "sending": sending, "payload": payload,
                    "messages": messages})
    return out


def decode_wire(pcap_path):
    """Every message in the pcap, in wire order:
    [((channel, version, seq, index, sendingTime), message bytes)]."""
    return [((p["channel"], p["version"], p["seq"], index, p["sending"]), data)
            for p in read_packets(pcap_path) for index, data in enumerate(p["messages"])]


def top5(builder, security_id):
    levels = {"BUY": [], "SELL": []}
    for side, price, qty, _ in builder.resting(security_id):
        side_levels = levels[side]
        if side_levels and side_levels[-1][0] == price:
            side_levels[-1][1] += qty
            side_levels[-1][2] += 1
        else:
            side_levels.append([price, qty, 1])
    return levels["BUY"][:5], levels["SELL"][:5]
