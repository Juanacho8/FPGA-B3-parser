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
wrong for every instrument with a message in it, from that packet on (until
a snapshot, v2). A message with no instrument (SecurityGroupPhase) makes
every instrument on its channel unreliable.

Must not: bypass the layers; scenarios drive the normal pipeline or apply
documented packet-level faults.
See docs/FEED_GENERATOR_CONTEXT.md section 9.
"""

import random
from dataclasses import dataclass

from b3gen.packet import HEARTBEAT_SEQUENCE_NUMBER


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
        if seq != HEARTBEAT_SEQUENCE_NUMBER:
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
    out = {}
    for position in gap_positions:  # wire order, so the first one wins
        channel = packetized.packets[position][1]
        version, seq = packetized.headers[position]
        for tag in tags_at.get(position, []):
            instrument = published[tag].instrument
            names = channel_instruments[channel] if instrument is None else [instrument]
            for name in names:
                out.setdefault(name, (channel, version, seq))
    return out
