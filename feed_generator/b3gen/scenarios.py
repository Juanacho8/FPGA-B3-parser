"""Support: scenario injection.

Loss, gaps, idle heartbeats, weekly start, engine failover, ChannelReset,
single-instrument reset, new trading session, edge orders, schema
evolution, rare messages and malformed packets. Each scenario goes in its
own output file, separate from the clean main file.

Built so far (step 7a):

* Feed B: every Feed A packet, byte for byte (header sendingTime included),
  later by base_delay_ns + uniform jitter in [0, jitter_ns] from
  random.Random(f"{seed}:feedB"). B keeps each channel's order: a packet
  never leaves before the previous one on its channel. Only the pcap
  timestamp moves.
* Loss: bursts of sequenced packets missing from Feed A only.
* Gaps: bursts of sequenced packets missing from both feeds, or (gap_one)
  one chosen packet holding messages for exactly one instrument.
  Burst model for both: at each sequenced packet of a channel a burst starts
  with probability burst_rate; its length is geometric with mean mean_burst.
  Loss and gaps have their own RNGs (f"{seed}:loss", f"{seed}:gap").
  Heartbeats are never dropped: their sequenceNumber is 0, so a manifest
  could not name them.
* Idle: a pause in the order flow (main.py, via OrderFlow.pause), so the
  packetizer sends heartbeats on both feeds.

Step 7b: Reset describes a restart or reset that main.py drives through the
normal layers (publisher.sequence_reset/channel_reset/rebuild, packetizer
bump_version/stream_down):
* failover: per channel SequenceReset, alone in the old version's last
  packet (sequenceNumber X+1, version N); then version N+1 from 1, and each
  instrument with a book rebuilt (LAYOUTS.md 12.1, 12.9).
* channel_reset: ChannelReset on one channel, then its rebuilds. No version change.
* instrument_reset: one instrument's rebuild (EmptyBook + orders).
* session_change: end of session, the stream down overnight, 3 start-of-day
  heartbeats, then the next day's session start (TRADING_SESSION_CHANGE).
* weekly_start: Friday's end of session, the stream down over the weekend,
  3 start-of-day heartbeats, per channel SequenceReset and version +1,
  ChannelReset, rebuilds of the orders carried over, then Monday's session.
v1 rebuilds every instrument before live flow resumes (open item J).

unreliable_books: after a packet missing from both feeds, a client's book is
wrong for every instrument with a book message in it, from that packet on
(until a snapshot, v2). Only channel-wide state messages (SecurityGroupPhase,
ChannelReset, SequenceReset) make every instrument on the channel
unreliable. News and unknown templates never make a book unreliable.

Step 7c:
* Inject: one-off actions main.py performs through the normal layers
  (mass_cancel, sweep, trade_bust, news, extend, unknown).
* malform: packet-level faults for the malformed file, one per packet, with
  what a client must do: process the complete messages before the fault,
  discard the rest of the packet, carry on with sequencing.

Must not: bypass the layers; scenarios drive the normal pipeline or apply
documented packet-level faults.
See docs/FEED_GENERATOR_CONTEXT.md section 9.
"""

import dataclasses
import random
import struct
from dataclasses import dataclass

from b3gen import layouts
from b3gen.packet import HEARTBEAT_SEQUENCE_NUMBER, PACKET_HEADER_SIZE

CHANNEL_WIDE = {layouts.TEMPLATES_BY_NAME[n].id for n in
                ("SecurityGroupPhase_10", "ChannelReset_11", "SequenceReset_1")}
NEWS = layouts.TEMPLATES_BY_NAME["News_5"].id
INJECT_KINDS = ("mass_cancel", "sweep", "trade_bust", "news", "extend", "unknown")
FAULTS = ("short", "length_past_end", "length_zero", "length_too_small", "bad_encoding",
          "truncated")


@dataclass(frozen=True)
class FeedBParams:
    base_delay_ns: int = 20_000
    jitter_ns: int = 10_000


@dataclass(frozen=True)
class BurstParams:
    burst_rate: float      # chance a burst starts at a given sequenced packet
    mean_burst: float      # mean burst length in packets (geometric, >= 1)


@dataclass(frozen=True)
class IdleParams:
    after_messages: int                  # pause once this many messages are published
    duration_ns: int = 3_500_000_000     # long enough for 3 heartbeats per channel


class ScenarioError(ValueError):
    """A scenario could not place what it promises (usually: the message
    target is too small)."""


RESET_KINDS = ("failover", "channel_reset", "instrument_reset", "session_change",
               "weekly_start")


@dataclass(frozen=True)
class Reset:
    """A restart or reset once after_messages messages are published
    (step 7b, docs/FEED_GENERATOR_CONTEXT.md section 9). target is the
    channel for channel_reset, the instrument for instrument_reset, unused
    otherwise."""
    kind: str
    after_messages: int
    target: object = None

    def __post_init__(self):
        if self.kind not in RESET_KINDS:
            raise ValueError(f"unknown reset {self.kind!r}; one of {RESET_KINDS}")


@dataclass(frozen=True)
class Inject:
    """A one-off action once after_messages messages are published.
    args by kind:
      mass_cancel: instrument, side
      sweep: instrument, side, orders (market order for the best N opposite orders)
      trade_bust: instrument (busts its latest trade)
      news: channel, instrument (None: market-wide), text_bytes, part_size,
            spread (messages between parts), block_extra, version
      extend: count (next Rested outcomes encoded with blockLength +8, version 18)
      unknown: template_id, body_bytes (inserted inside the next multi-message event)"""
    kind: str
    after_messages: int
    args: tuple = ()

    def __post_init__(self):
        if self.kind not in INJECT_KINDS:
            raise ValueError(f"unknown injection {self.kind!r}; one of {INJECT_KINDS}")

    @property
    def params(self):
        return dict(self.args)


@dataclass(frozen=True)
class Feeds:
    a: list                # (time_ns, channel, bytes) in wire order
    b: list
    missing_a: list        # (channel, sequenceVersion, sequenceNumber), wire order
    missing_b: list
    gap_positions: list    # packet positions missing from both feeds


def burst_positions(packetized, params, rng):
    """Packet positions dropped by the burst model, each channel in turn."""
    if params is None:
        return set()
    if params.mean_burst < 1 or not 0 <= params.burst_rate <= 1:
        raise ValueError(f"bad burst parameters {params}")
    per_channel = {}
    for position, ((_, channel, _), (_, seq)) in enumerate(
            zip(packetized.packets, packetized.headers)):
        if seq not in (HEARTBEAT_SEQUENCE_NUMBER, None):  # None: a stray datagram
            per_channel.setdefault(channel, []).append(position)
    keep_going = 1 - 1 / params.mean_burst
    dropped = set()
    for channel in sorted(per_channel):
        positions = per_channel[channel]
        i = 0
        while i < len(positions):
            if rng.random() < params.burst_rate:
                length = 1
                while rng.random() < keep_going:
                    length += 1
                dropped.update(positions[i:i + length])
                i += length
            else:
                i += 1
    return dropped


def feed_b_times(packetized, params, rng):
    """Feed B time for every packet, in Feed A wire order."""
    last = {}
    times = []
    for t, channel, _ in packetized.packets:
        tb = t + params.base_delay_ns + rng.randint(0, params.jitter_ns)
        tb = max(tb, last.get(channel, tb))
        last[channel] = tb
        times.append(tb)
    return times


def single_instrument_packet(packetized, published):
    """The first sequenced packet from the middle of the run on whose
    messages all belong to one instrument (not a channel-wide message)."""
    tags_at = {}
    for tag, p in packetized.placements.items():
        tags_at.setdefault(p.packet_position, []).append(tag)
    n = len(packetized.packets)
    for position in range(n // 2, n):
        names = {published[tag].instrument for tag in tags_at.get(position, [])}
        if len(names) == 1 and None not in names:
            return position
    raise ValueError("no packet with messages for exactly one instrument")


def build_feeds(packetized, seed, *, feed_b=FeedBParams(), loss=None, gap=None,
                gap_positions=()):
    """gap_positions: packets missing from both feeds besides the random gaps."""
    lost = burst_positions(packetized, loss, random.Random(f"{seed}:loss"))
    gapped = burst_positions(packetized, gap, random.Random(f"{seed}:gap")) | set(gap_positions)
    b_times = feed_b_times(packetized, feed_b, random.Random(f"{seed}:feedB"))

    def key(position):
        version, seq = packetized.headers[position]
        return (packetized.packets[position][1], version, seq)

    a, b = [], []
    for position, (t, channel, data) in enumerate(packetized.packets):
        if position not in lost and position not in gapped:
            a.append((t, channel, data))
        if position not in gapped:
            b.append((b_times[position], position, channel, data))
    b.sort()  # merge channels by time; ties keep Feed A order
    return Feeds(a=a, b=[(t, channel, data) for t, _, channel, data in b],
                 missing_a=[key(p) for p in sorted(lost | gapped)],
                 missing_b=[key(p) for p in sorted(gapped)],
                 gap_positions=sorted(gapped))


def unreliable_books(gap_positions, packetized, published, channel_instruments):
    """{instrument: (channel, sequenceVersion, sequenceNumber) of the first
    packet missing from both feeds that makes its book unreliable}.

    channel_instruments: channel -> instrument names on it."""
    tags_at = {}
    for tag, p in packetized.placements.items():
        tags_at.setdefault(p.packet_position, []).append(tag)
    return unreliable_from_lost(
        [(position, sorted(tags_at.get(position, []))) for position in gap_positions],
        packetized, published, channel_instruments)


def unreliable_from_lost(lost, packetized, published, channel_instruments):
    """lost: [(packet position, [tags a client never saw])] in wire order."""
    out = {}
    for position, tags in lost:  # wire order, so the first one wins
        channel = packetized.packets[position][1]
        version, seq = packetized.headers[position]
        for tag in tags:
            msg = published[tag]
            if msg.template_id in CHANNEL_WIDE:
                names = channel_instruments[channel]
            elif msg.instrument is None or msg.template_id == NEWS:
                names = []  # News and unknown templates never touch a book
            else:
                names = [msg.instrument]
            for name in names:
                out.setdefault(name, (channel, version, seq))
    return out


def _message_starts(payload):
    """Offsets of the messages in one of our own (well-formed) packets."""
    starts, pos = [], PACKET_HEADER_SIZE
    while pos < len(payload):
        starts.append(pos)
        pos += struct.unpack_from("<H", payload, pos)[0]
    return starts


def malform(packetized, seed):
    """One fault per chosen packet, plus a stray short datagram.

    Returns (packetized with the faulty packets and positions shifted for
    the stray datagram, faults, lost) where faults lists, per fault, its
    kind, packet, byte offset and messages before it, and lost is
    [(position, [discarded tags])] for unreliable_from_lost."""
    tags_at = {}
    for tag, p in packetized.placements.items():
        tags_at.setdefault(p.packet_position, []).append(tag)
    eligible = [pos for pos, (_, seq) in enumerate(packetized.headers)
                if seq != HEARTBEAT_SEQUENCE_NUMBER and pos >= len(packetized.packets) // 3
                and len(_message_starts(packetized.packets[pos][2])) >= 3]
    kinds = [k for k in FAULTS if k != "short"]
    if len(eligible) < 10 * len(kinds):
        raise ValueError("run too short for the malformed file")
    chosen = dict(zip(eligible[::10], kinds))
    packets = list(packetized.packets)
    faults, lost = [], []
    for pos, kind in chosen.items():
        t, channel, payload = packets[pos]
        data = bytearray(payload)
        starts = _message_starts(payload)
        index = len(starts) - 1 if kind == "truncated" else 1
        offset = starts[index]
        if kind == "length_past_end":
            struct.pack_into("<H", data, offset, len(data) - offset + 100)
        elif kind == "length_zero":
            struct.pack_into("<H", data, offset, 0)
        elif kind == "length_too_small":
            struct.pack_into("<H", data, offset, 8)
        elif kind == "bad_encoding":
            struct.pack_into("<H", data, offset + 2, 0xEB51)
        else:  # truncated inside the last message's 12-byte header
            data = data[:offset + 5]
        packets[pos] = (t, channel, bytes(data))
        version, seq = packetized.headers[pos]
        faults.append({"fault": kind, "channel": channel, "sequenceVersion": version,
                       "sequenceNumber": seq, "offset": offset, "messages_before": index})
        lost.append((pos, sorted(tags_at[pos])[index:]))

    # The stray datagram: 10 bytes on channel 68's group, right after a
    # packet in the middle of the file. It has no header, so it uses up no
    # sequence number.
    rng = random.Random(f"{seed}:malformed")
    after = next(pos for pos in range(len(packets) // 2, len(packets))
                 if packetized.headers[pos][1] != HEARTBEAT_SEQUENCE_NUMBER)
    t, channel, _ = packets[after]
    version, seq = packetized.headers[after]
    packets.insert(after + 1, (t, channel, bytes(rng.randrange(256) for _ in range(10))))
    headers = list(packetized.headers)
    headers.insert(after + 1, (None, None))
    faults.append({"fault": "short", "channel": channel, "after": [channel, version, seq],
                   "length": 10})

    def shift(p):
        return dataclasses.replace(p, packet_position=p.packet_position + 1) \
            if p.packet_position > after else p
    shifted = dataclasses.replace(
        packetized, packets=packets, headers=headers,
        placements={tag: shift(p) for tag, p in packetized.placements.items()},
        heartbeats=[dataclasses.replace(hb, placement=shift(hb.placement))
                    for hb in packetized.heartbeats])
    lost = [(pos + 1 if pos > after else pos, tags) for pos, tags in sorted(lost)]
    faults.sort(key=lambda f: (f["channel"], f.get("sequenceVersion", 0), f.get("sequenceNumber", 0)))
    return shifted, faults, lost
