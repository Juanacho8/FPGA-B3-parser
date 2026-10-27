"""Independent wire helpers shared by the end-to-end tests (no b3gen)."""

import struct
from pathlib import Path

import pcap_reader

HEADER = struct.Struct("<BBHIQ")


MESSAGE_HEADER = 12
ENCODING_TYPE = 0xEB50


def split_messages(payload):
    """(complete messages, fault) for one UDP payload, as a client must
    read it: header first, then messages by messageLength. fault is None or
    (kind, byte offset); everything from the fault on is discarded. Never
    loops: a messageLength under 12 (including 0) stops the walk."""
    if len(payload) < HEADER.size:
        return [], ("short", 0)
    messages, pos = [], HEADER.size
    while pos < len(payload):
        if len(payload) - pos < MESSAGE_HEADER:
            return messages, ("truncated", pos)
        length, encoding = struct.unpack_from("<HH", payload, pos)
        if length == 0:
            return messages, ("length_zero", pos)
        if length < MESSAGE_HEADER:
            return messages, ("length_too_small", pos)
        if encoding != ENCODING_TYPE:
            return messages, ("bad_encoding", pos)
        if pos + length > len(payload):
            return messages, ("length_past_end", pos)
        messages.append(payload[pos:pos + length])
        pos += length
    return messages, None


def read_packets(pcap_path):
    """[{"time", "channel", "version", "seq", "sending", "payload", "messages",
    "fault"}] in file order, decoded from the pcap alone. A datagram too
    short for the packet header has channel, version, seq and sending None."""
    _, records = pcap_reader.read_pcap(Path(pcap_path).read_bytes())
    out = []
    for rec in records:
        payload = rec["payload"]
        messages, fault = split_messages(payload)
        if fault and fault[0] == "short":
            channel = version = seq = sending = None
        else:
            channel, _, version, seq, sending = HEADER.unpack_from(payload, 0)
        out.append({"time": rec["time_ns"], "channel": channel, "version": version,
                    "seq": seq, "sending": sending, "payload": payload,
                    "messages": messages, "fault": fault})
    return out


def in_sequence_order(packets):
    """packets with each channel's sequenced packets put back in
    (version, seq) order in the slots that channel's sequenced packets
    used: what a client with a reorder buffer applies. Heartbeats, stray
    datagrams and the interleaving of channels stay where they are."""
    slots = {}
    for i, p in enumerate(packets):
        if p["seq"]:
            slots.setdefault(p["channel"], []).append(i)
    out = list(packets)
    for indices in slots.values():
        ordered = sorted((packets[i] for i in indices), key=lambda p: (p["version"], p["seq"]))
        for i, p in zip(indices, ordered):
            out[i] = p
    return out


def decode_wire(pcap_path):
    """Every message in the pcap, in wire order:
    [((channel, version, seq, index, sendingTime), message bytes)]."""
    out = []
    for p in read_packets(pcap_path):
        assert p["fault"] is None, p["fault"]
        out += [((p["channel"], p["version"], p["seq"], index, p["sending"]), data)
                for index, data in enumerate(p["messages"])]
    return out


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
