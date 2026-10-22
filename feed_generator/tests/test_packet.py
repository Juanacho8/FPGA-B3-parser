"""Packetizer tests.

Packet payloads are split back into messages using messageLength only (the
first 2 bytes of each message), never decoded: the packetizer must treat
messages as opaque bytes.
"""

import json
import random
import struct
from pathlib import Path

import pytest

from b3gen import encode
from b3gen.packet import PacketError, Packetizer

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "b3_sample_packet"
SECOND = 1_000_000_000


def header(packet):
    channel, reserved, version, seq, sending = struct.unpack_from("<BBHIQ", packet, 0)
    return {"channel": channel, "reserved": reserved, "version": version,
            "seq": seq, "sending": sending}


def split_messages(packet):
    messages, pos = [], 16
    while pos < len(packet):
        (length,) = struct.unpack_from("<H", packet, pos)
        messages.append(packet[pos:pos + length])
        pos += length
    assert pos == len(packet)
    return messages


def blob(length, rng):
    """An opaque message: valid messageLength, random body."""
    return struct.pack("<H", length) + bytes(rng.randrange(256) for _ in range(length - 2))


def is_heartbeat(packet):
    return header(packet)["seq"] == 0


def real_packets(packets, channel=None):
    return [p for p in packets if not is_heartbeat(p[2]) and (channel is None or p[1] == channel)]


def heartbeats(packets, channel=None):
    return [p for p in packets if is_heartbeat(p[2]) and (channel is None or p[1] == channel)]


# ---------------------------------------------------------------------------
# B3 sample packet
# ---------------------------------------------------------------------------

def test_rebuilds_b3_sample_packet():
    hex_text = "".join(line for line in (FIXTURE / "packet.hex").read_text().splitlines()
                       if not line.startswith("#"))
    expected = bytes.fromhex(hex_text)
    decoded = json.loads((FIXTURE / "decoded.json").read_text())
    ph = decoded["packet_header"]
    t = ph["sendingTime"]  # also both messages' transactTime

    p = Packetizer([ph["channelNumber"]], start_ns=t, window_ns=5000,
                   initial_sequence_number=ph["sequenceNumber"])
    for msg in decoded["messages"]:
        p.add(t, ph["channelNumber"], encode.encode(msg["message_header"]["templateId"],
                                                    msg["fields"]))
    packets = p.finish(end_ns=t).packets

    assert len(packets) == 1
    time_ns, channel, packet = packets[0]
    assert (time_ns, channel) == (t, 55)
    assert packet == expected


# ---------------------------------------------------------------------------
# Properties over a seeded random run
# ---------------------------------------------------------------------------

def random_run(window_ns, seed=7, n=3000):
    rng = random.Random(seed)
    channels = [68, 70]
    p = Packetizer(channels, start_ns=0, window_ns=window_ns)
    inputs = {c: [] for c in channels}
    t = 0
    for _ in range(n):
        # Mostly bursts (0-3 us apart), sometimes pauses up to 2.5 s.
        t += rng.choice([0, rng.randrange(3000), rng.randrange(3000),
                         rng.randrange(2_500_000_000)])
        channel = rng.choice(channels)
        message = blob(rng.choice([20, 56, 72, 200, 600, 1384]), rng)
        p.add(t, channel, message)
        inputs[channel].append((t, message))
    return p.finish(end_ns=t + 3 * SECOND).packets, inputs


@pytest.mark.parametrize("window_ns", [0, 5000, None])
def test_packet_properties(window_ns):
    packets, inputs = random_run(window_ns)
    assert packets == sorted(packets, key=lambda p: p[0])
    for channel, sent in inputs.items():
        real = real_packets(packets, channel)
        # Never over 1400 bytes; header matches; sequence numbers consecutive.
        assert all(len(p) <= 1400 for _, _, p in real)
        assert [header(p)["seq"] for _, _, p in real] == list(range(1, len(real) + 1))
        assert all(header(p)["channel"] == channel and header(p)["version"] == 1
                   for _, _, p in real)
        # Messages never split or altered, and stay in order.
        assert [m for _, _, p in real for m in split_messages(p)] == [m for _, m in sent]
        # sendingTime >= each message time; non-decreasing per channel.
        times = iter(t for t, _ in sent)
        for time_ns, _, p in real:
            assert header(p)["sending"] == time_ns
            for _ in split_messages(p):
                assert next(times) <= time_ns
        # Heartbeats included: sendingTime never decreases on a channel.
        sending = [t for t, c, _ in packets if c == channel]
        assert len(real) > 0
        if window_ns is not None:
            # The run has 2.5 s pauses, so heartbeats must appear. With None a
            # packet is always open, so there are none (confirmed choice 2).
            assert len(sending) > len(real)
        assert sending == sorted(sending)


def test_window_zero_is_one_message_per_packet():
    packets, inputs = random_run(0)
    assert len(real_packets(packets)) == sum(len(v) for v in inputs.values())


def test_window_none_always_fills():
    # Every packet except the last on a channel closed because the next
    # message did not fit.
    packets, _ = random_run(None)
    for channel in (68, 70):
        real = real_packets(packets, channel)
        for (_, _, p), (_, _, nxt) in zip(real, real[1:]):
            assert len(p) + len(split_messages(nxt)[0]) > 1400


def test_window_bounds_message_spread():
    rng = random.Random(1)
    p = Packetizer([68], start_ns=0, window_ns=5000)
    for i in range(200):
        p.add(i * 1700, 68, blob(30, rng))
    real = real_packets(p.finish(end_ns=200 * 1700).packets)
    # 1700 ns apart, window 5000: messages at t0, +1700, +3400 share a packet.
    assert [len(split_messages(pk)) for _, _, pk in real[:-1]] == [3] * (len(real) - 1)
    for time_ns, _, pk in real[:-1]:
        assert header(pk)["sending"] == time_ns


def test_message_at_window_boundary_starts_new_packet():
    p = Packetizer([68], start_ns=0, window_ns=5000)
    p.add(0, 68, b"\x04\x00\x00\x00")
    p.add(5000, 68, b"\x04\x00\x00\x00")
    real = real_packets(p.finish(end_ns=5000).packets)
    assert [header(pk)["sending"] for _, _, pk in real] == [5000, 5000]
    assert len(real) == 2


def test_finish_before_last_message_raises():
    p = Packetizer([68], start_ns=0)
    p.add(100, 68, b"\x04\x00\x00\x00")
    with pytest.raises(PacketError, match="before the last message"):
        p.finish(end_ns=99).packets


def test_time_going_backwards_raises():
    p = Packetizer([68], start_ns=0)
    p.add(100, 68, b"\x04\x00\x00\x00")
    with pytest.raises(PacketError):
        p.add(99, 68, b"\x04\x00\x00\x00")


def test_unknown_channel_raises():
    with pytest.raises(PacketError, match="unknown channel"):
        Packetizer([68], start_ns=0).add(0, 70, b"\x04\x00\x00\x00")


# ---------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------

def sequence_next_seq_no(packet):
    (message,) = split_messages(packet)
    length, _, block_length, template_id, _, _ = struct.unpack_from("<HHHHHH", message, 0)
    assert (length, block_length, template_id) == (16, 4, 2)
    return struct.unpack_from("<I", message, 12)[0]


def test_stale_packet_closes_before_heartbeats():
    # One message, a 3.5 s gap, then another: the first packet closes at
    # t0 + window, heartbeats follow at close + 1 s, + 2 s, + 3 s.
    p = Packetizer([68], start_ns=0, window_ns=5000)
    p.add(0, 68, encode.sequence_reset())
    p.add(3_500_000_000, 68, encode.sequence_reset())
    packets = p.finish(end_ns=3_500_000_000).packets

    real = real_packets(packets)
    assert [t for t, _, _ in real] == [5000, 3_500_000_000]
    hb = heartbeats(packets)
    assert [t for t, _, _ in hb] == [5000 + SECOND, 5000 + 2 * SECOND, 5000 + 3 * SECOND]
    for t, _, pk in hb:
        assert header(pk) == {"channel": 68, "reserved": 0, "version": 1, "seq": 0, "sending": t}
        assert sequence_next_seq_no(pk) == 2


def test_heartbeats_from_start_and_until_end():
    # A channel with no traffic heartbeats from start_ns, up to end_ns.
    p = Packetizer([68, 70], start_ns=0)
    p.add(0, 70, encode.sequence_reset())
    packets = p.finish(end_ns=3 * SECOND).packets
    assert [t for t, _, _ in heartbeats(packets, 68)] == [SECOND, 2 * SECOND, 3 * SECOND]
    assert all(sequence_next_seq_no(pk) == 1 for _, _, pk in heartbeats(packets, 68))
    # Channel 70: packet at 5000 ns, then heartbeats 1 s apart until the end.
    assert [t for t, _, _ in heartbeats(packets, 70)] == [5000 + SECOND, 5000 + 2 * SECOND]


def test_no_heartbeats_while_traffic_flows():
    p = Packetizer([68], start_ns=0)
    for i in range(21):
        p.add(i * SECOND // 2, 68, encode.sequence_reset())
    assert heartbeats(p.finish(end_ns=10 * SECOND).packets) == []


def test_heartbeat_does_not_consume_sequence_number():
    p = Packetizer([68], start_ns=0)
    p.add(0, 68, encode.sequence_reset())
    p.add(5 * SECOND, 68, encode.sequence_reset())
    real = real_packets(p.finish(end_ns=5 * SECOND).packets)
    assert [header(pk)["seq"] for _, _, pk in real] == [1, 2]


# ---------------------------------------------------------------------------
# sequenceVersion
# ---------------------------------------------------------------------------

def test_version_bump_restarts_sequence_number():
    p = Packetizer([68], start_ns=0, window_ns=5000)
    p.add(0, 68, encode.sequence_reset())
    p.add(10_000, 68, encode.sequence_reset())
    p.add(11_000, 68, encode.sequence_reset())   # open when the bump comes
    p.bump_version(12_000, 68)
    p.add(20_000, 68, encode.sequence_reset())
    p.add(30_000, 68, encode.sequence_reset())
    real = real_packets(p.finish(end_ns=30_000).packets)
    assert [(header(pk)["version"], header(pk)["seq"]) for _, _, pk in real] == \
        [(1, 1), (1, 2), (2, 1), (2, 2)]
    # The open packet closed at the bump time, with both its messages.
    assert real[1][0] == 12_000 and len(split_messages(real[1][2])) == 2


def test_heartbeat_after_bump_points_at_sequence_one():
    p = Packetizer([68], start_ns=0)
    p.add(0, 68, encode.sequence_reset())
    p.bump_version(10_000, 68)
    hb = heartbeats(p.finish(end_ns=2 * SECOND).packets)
    assert hb and all(header(pk)["version"] == 2 and sequence_next_seq_no(pk) == 1
                      for _, _, pk in hb)


def test_version_rolls_back_to_one_after_65534():
    p = Packetizer([68], start_ns=0)
    p.channels[68].sequence_version = 65534
    p.bump_version(0, 68)
    p.add(1, 68, encode.sequence_reset())
    (_, _, pk), = real_packets(p.finish(end_ns=1).packets)
    assert header(pk)["version"] == 1
