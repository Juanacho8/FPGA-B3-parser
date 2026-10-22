"""Layer 4: packetizer.

Groups already-encoded messages into packets per channel, stamps the 16-byte
packet header and emits heartbeats. Never decodes or changes message bytes.

Packetization is option 3 (docs/FEED_GENERATOR_CONTEXT.md section 7): per
channel, the open packet closes when the next message would push it past
1400 bytes, or when window_ns has passed since its first message.
window_ns = 0 gives one message per packet; None means always fill.

The packet's close time is its sendingTime:
* window expired: t0 + window_ns (when it would really have left),
* next message didn't fit: that message's arrival time,
* end of input / version bump: min(t0 + window_ns, that time).
So sendingTime >= every message time in the packet, and never decreases
within a channel.

Sequencing: sequenceNumber +1 per packet per channel, sequenceVersion starts
at 1. bump_version() increments the version (rolling back to 1 after 65534)
and restarts sequenceNumber at 1. Given a final message (SequenceReset), it
first sends that message alone as the old version's last packet, at the next
sequenceNumber (LAYOUTS.md section 12.1).

Heartbeats: when a channel has no open packet for more than heartbeat_ns,
a packet holding only a Sequence message is sent, with packet
sequenceNumber 0 and nextSeqNo = the next real packet's number. Repeats
every heartbeat_ns while idle. Idle time counts from the last packet sent on
the channel, or from start_ns before the first one. stream_down() stops a
channel's heartbeats until it comes back up (weekend, overnight); the first
start-of-day heartbeat goes out as it comes up.

Streaming: given a sink, each packet is handed over (in final wire order,
with its position) as soon as no channel can still produce an earlier one,
instead of being kept until finish(). A channel's earliest possible next
packet is its open packet's first message time, or, with none open, the
earlier of its next heartbeat and the current time. In streaming mode the
caller must add messages in nondecreasing time across all channels.

Feed B (a delayed copy) and packet loss belong to scenarios.py, not here.

Must not: change message content.
See docs/FEED_GENERATOR_CONTEXT.md section 7 and spec/LAYOUTS.md sections 3 and 12.1.
"""

import heapq
from dataclasses import dataclass

from b3gen import encode, layouts

MAX_PACKET_SIZE = encode.MAX_PACKET_SIZE
PACKET_HEADER_SIZE = encode.PACKET_HEADER_SIZE
MAX_SEQUENCE_VERSION = 65534  # rolls back to 1 after this (LAYOUTS.md section 3)
HEARTBEAT_SEQUENCE_NUMBER = 0


class PacketError(ValueError):
    """Input the packetizer cannot accept."""


@dataclass(frozen=True)
class Placement:
    """Where a message landed on the wire. packet_position is the packet's
    index in Packetized.packets (wire order across channels)."""
    channel: int
    sequence_version: int
    sequence_number: int
    index: int               # message index within the packet, from 0
    sending_time: int
    packet_position: int


@dataclass(frozen=True)
class Heartbeat:
    """A heartbeat packet's single Sequence message."""
    placement: Placement
    next_seq_no: int


@dataclass(frozen=True)
class Packetized:
    packets: list            # (time_ns, channel, packet bytes), wire order
    placements: dict         # tag -> Placement, for every tagged message
    heartbeats: list         # Heartbeat, in wire order
    headers: list = None     # (sequence_version, sequence_number) per packet


class _Channel:
    def __init__(self, number, start_ns, initial_sequence_number):
        self.number = number
        self.sequence_version = 1
        self.next_sequence_number = initial_sequence_number
        self.last_send_ns = start_ns   # heartbeats count from here
        self.last_message_ns = start_ns
        self.open_t0 = None            # first message time of the open packet
        self.open_messages = []
        self.open_tags = []
        self.open_size = PACKET_HEADER_SIZE


class Packetizer:
    def __init__(self, channels, start_ns, *, window_ns=5000,
                 heartbeat_ns=1_000_000_000, initial_sequence_number=1, sink=None):
        """sink(position, time_ns, channel, sequence_version, sequence_number,
        contents, data): streaming mode (see the module docstring). contents
        is the packet's tags, or a heartbeat's nextSeqNo."""
        if window_ns is not None and window_ns < 0:
            raise PacketError("window_ns must be >= 0 or None")
        if heartbeat_ns <= 0:
            raise PacketError("heartbeat_ns must be > 0")
        self.window_ns = window_ns
        self.heartbeat_ns = heartbeat_ns
        self.start_ns = start_ns
        self.channels = {c: _Channel(c, start_ns, initial_sequence_number) for c in channels}
        self._packets = []    # (time_ns, channel, emit order, bytes)
        self._contents = []   # per emitted packet: (sequence_version, seq, tags or next_seq_no)
        self._finished = False
        self._sink = sink
        self._ready = []      # streaming: heap of packets not handed over yet
        self._emitted = 0
        self._released = 0
        self._now = start_ns  # latest time seen on any channel
        self._heartbeat_list = []

    # -- public -------------------------------------------------------------

    def add(self, time_ns, channel, message, tag=None):
        """Queue one encoded message published at time_ns on channel. A tag
        (any hashable, unique) gets the message's Placement in finish()."""
        ch = self._channel(channel)
        if not isinstance(message, (bytes, bytearray)):
            raise PacketError("message must be bytes")
        if PACKET_HEADER_SIZE + len(message) > MAX_PACKET_SIZE:
            raise PacketError(f"message of {len(message)} bytes cannot fit in a packet")
        if time_ns < ch.last_message_ns:
            raise PacketError(f"channel {channel}: time {time_ns} before {ch.last_message_ns}")
        self._advance(time_ns)
        ch.last_message_ns = time_ns

        if ch.open_t0 is not None:
            if self.window_ns is not None and time_ns - ch.open_t0 >= self.window_ns:
                # Window expired before this message arrived: the packet left
                # at t0 + window, and any idle gap is measured from there.
                self._close(ch, ch.open_t0 + self.window_ns)
            elif ch.open_size + len(message) > MAX_PACKET_SIZE:
                # Does not fit: the packet leaves as this message arrives.
                self._close(ch, time_ns)

        if ch.open_t0 is None:
            self._heartbeats(ch, until_ns=time_ns, inclusive=False)
            ch.open_t0 = time_ns
        ch.open_messages.append(bytes(message))
        ch.open_tags.append(tag)
        ch.open_size += len(message)
        self._release()

    def bump_version(self, time_ns, channel, final_message=None, final_tag=None):
        """Close the open packet, send final_message (if any) alone as the
        old version's last packet, then sequenceVersion +1 and
        sequenceNumber 1."""
        ch = self._channel(channel)
        if time_ns < ch.last_message_ns:
            raise PacketError(f"channel {channel}: time {time_ns} before {ch.last_message_ns}")
        self._advance(time_ns)
        ch.last_message_ns = time_ns
        self._close_by(ch, time_ns)
        self._heartbeats(ch, until_ns=time_ns, inclusive=False)
        if final_message is not None:
            self._emit(ch, time_ns, ch.next_sequence_number, bytes(final_message), [final_tag])
            ch.next_sequence_number += 1
        ch.sequence_version = 1 if ch.sequence_version >= MAX_SEQUENCE_VERSION \
            else ch.sequence_version + 1
        ch.next_sequence_number = 1
        self._release()

    def stream_down(self, time_ns, channel, up_ns):
        """Close the open packet at time_ns, send the heartbeats due before
        it, then nothing until up_ns, when start-of-day heartbeats begin."""
        ch = self._channel(channel)
        if not time_ns <= up_ns or time_ns < ch.last_message_ns:
            raise PacketError(f"channel {channel}: bad stream_down {time_ns}..{up_ns}")
        self._advance(time_ns)
        self._close_by(ch, time_ns)
        self._heartbeats(ch, until_ns=time_ns, inclusive=False)
        ch.last_message_ns = up_ns
        ch.last_send_ns = up_ns - self.heartbeat_ns
        self._release()

    def finish(self, end_ns):
        """Close every open packet, emit trailing heartbeats up to end_ns and
        return Packetized: packets (time_ns, channel, bytes) in wire order,
        placements for tagged messages, and heartbeats."""
        latest = max(ch.last_message_ns for ch in self.channels.values())
        if end_ns < latest:
            raise PacketError(f"end_ns {end_ns} is before the last message time {latest}")
        for ch in self.channels.values():
            self._close_by(ch, end_ns)
            self._heartbeats(ch, until_ns=end_ns, inclusive=True)
        self._finished = True
        if self._sink is not None:
            self._release(everything=True)
            return Packetized([], {}, self._heartbeat_list, None)
        order = sorted(self._packets)
        packets, placements, heartbeats, headers = [], {}, [], []
        for position, (t, c, emit, data) in enumerate(order):
            packets.append((t, c, data))
            version, seq, contents = self._contents[emit]
            headers.append((version, seq))
            if seq == HEARTBEAT_SEQUENCE_NUMBER:
                heartbeats.append(Heartbeat(Placement(c, version, seq, 0, t, position), contents))
                continue
            for index, tag in enumerate(contents):
                if tag is not None:
                    placements[tag] = Placement(c, version, seq, index, t, position)
        return Packetized(packets, placements, heartbeats, headers)

    # -- internals ----------------------------------------------------------

    def _advance(self, time_ns):
        if self._sink is not None and time_ns < self._now:
            raise PacketError(f"streaming: time {time_ns} before {self._now} on another channel")
        self._now = max(self._now, time_ns)

    def _watermark(self):
        """No packet still to come can be earlier than this."""
        earliest = []
        for ch in self.channels.values():
            if ch.open_t0 is not None:
                earliest.append(ch.open_t0)
            else:
                earliest.append(min(ch.last_send_ns + self.heartbeat_ns,
                                    max(ch.last_message_ns, self._now)))
        return min(earliest)

    def _release(self, everything=False):
        if self._sink is None:
            return
        limit = None if everything else self._watermark()
        while self._ready and (limit is None or self._ready[0][0] < limit):
            t, c, _, version, seq, contents, data = heapq.heappop(self._ready)
            if seq == HEARTBEAT_SEQUENCE_NUMBER:
                self._heartbeat_list.append(
                    Heartbeat(Placement(c, version, seq, 0, t, self._released), contents))
            self._sink(self._released, t, c, version, seq, contents, data)
            self._released += 1

    def _channel(self, channel):
        if self._finished:
            raise PacketError("packetizer already finished")
        if channel not in self.channels:
            raise PacketError(f"unknown channel {channel}")
        return self.channels[channel]

    def _close_by(self, ch, time_ns):
        """Close the open packet at the earlier of its window expiry and time_ns."""
        if ch.open_t0 is None:
            return
        if self.window_ns is None:
            self._close(ch, time_ns)
        else:
            self._close(ch, min(ch.open_t0 + self.window_ns, time_ns))

    def _close(self, ch, sending_time):
        payload = b"".join(ch.open_messages)
        self._emit(ch, sending_time, ch.next_sequence_number, payload, list(ch.open_tags))
        ch.next_sequence_number += 1
        ch.open_t0 = None
        ch.open_messages = []
        ch.open_tags = []
        ch.open_size = PACKET_HEADER_SIZE

    def _heartbeats(self, ch, until_ns, inclusive):
        """Heartbeats every heartbeat_ns after the last send, before
        until_ns (or up to it, for the end of input)."""
        t = ch.last_send_ns + self.heartbeat_ns
        while t < until_ns or (inclusive and t == until_ns):
            message = encode.sequence(nextSeqNo=ch.next_sequence_number)
            self._emit(ch, t, HEARTBEAT_SEQUENCE_NUMBER, message, ch.next_sequence_number)
            t += self.heartbeat_ns

    def _emit(self, ch, sending_time, sequence_number, payload, contents):
        header = bytearray(PACKET_HEADER_SIZE)
        encode.write_members(header, 0, layouts.PACKET_HEADER, {
            "channelNumber": ch.number,
            "reserved": 0,
            "sequenceVersion": ch.sequence_version,
            "sequenceNumber": sequence_number,
            "sendingTime": sending_time,
        }, f"channel {ch.number} packet header", use_nulls=False)
        ch.last_send_ns = sending_time
        if self._sink is not None:
            heapq.heappush(self._ready, (sending_time, ch.number, self._emitted, ch.sequence_version,
                                         sequence_number, contents, bytes(header) + payload))
            self._emitted += 1
            return
        self._contents.append((ch.sequence_version, sequence_number, contents))
        self._packets.append((sending_time, ch.number, len(self._packets), bytes(header) + payload))
