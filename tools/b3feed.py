"""b3feed: one feed's answer-key messages in the feed's own packet order and time.

What it does:
  drive() walks a feed's pcap (read with the generator's independent reader,
  feed_generator/tests/pcap_reader.py) datagram by datagram, in capture
  order, and matches each with the feed's view of intent.jsonl
  (tools/b3intent.py): per channel, the next packet of the view is the next
  datagram of that channel the decoder accepts. For each datagram, in order:
    on_time(t)      before anything else (port 2's STATUS ticks)
    replay.advance(t), then on_after_time()   (the silence rule)
    replay.saw(channel, t)                     (any datagram with a header)
    replay.packet(rows, on_row) and on_after_packet()   if the view holds it
  A datagram the view does not hold (a short datagram, a duplicate, a late
  packet) only advances time. Feed B is handled the same way, so its answer
  keys follow Feed B's real order across channels.
  Raises b3replay.Broken if a packet of the view never comes up: on the
  reorder set (b3intent.EXPECTED_FAILURES) the view's window release order
  is not the pcap's order, until the window exists (step 11).

What it must not do:
  Import b3gen, or decode B3 messages: the messages are the intent log's.
"""

import struct
import sys
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "feed_generator" / "tests"))

import b3intent        # noqa: E402
import b3replay        # noqa: E402
import pcap_reader     # noqa: E402  (independent of b3gen and of the C++)

PACKET_HEADER = struct.Struct("<BBHIQ")


def datagrams(pcap):
    """[(capture time ns, UDP payload)] of a pcap, in capture order."""
    _, records = pcap_reader.read_pcap(Path(pcap).read_bytes())
    return [(r["time_ns"], r["payload"]) for r in records]


def drive(intent, manifest, feed, pcap, replay, on_row=None, on_time=None, on_after_time=None,
          on_after_packet=None):
    view = {}
    for pkt in b3replay.packets(r for _, r in b3intent.feed_rows(intent, manifest, feed)):
        view.setdefault(pkt[0]["channel"], deque()).append(pkt)
    for t, payload in datagrams(pcap):
        if on_time:
            on_time(t)
        replay.advance(t)
        if on_after_time:
            on_after_time()
        if len(payload) < PACKET_HEADER.size:
            continue
        ch, _, ver, seq, _ = PACKET_HEADER.unpack_from(payload, 0)
        replay.saw(ch, t)
        queue = view.get(ch)
        if not queue or b3intent.packet_key(queue[0][0]) != (ch, ver, seq):
            continue          # not in the view: dropped by the sequencer
        replay.packet(queue.popleft(), on_row=on_row)
        if on_after_packet:
            on_after_packet()
    left = sum(len(q) for q in view.values())
    if left:
        raise b3replay.Broken(f"{left} packets of the feed's view never met in {Path(pcap).name}")
