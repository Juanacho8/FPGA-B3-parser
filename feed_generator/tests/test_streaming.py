"""Streaming: the packetizer's sink hands over exactly what finish() would
return, in the same order, whatever the input; the writer forgets what it
has written; a run that cannot place an injection says so."""

import dataclasses
import random

import pytest

from b3gen import golden, main, scenarios
from b3gen.packet import Packetizer


def drive(packetizer, seed):
    """A hostile input: three channels, bursts that fill packets, idle gaps
    long enough for heartbeats on some channels only, version bumps and a
    stream going down. Times never go backwards across channels."""
    rng = random.Random(seed)
    t = 1_000
    for step in range(3000):
        r = rng.random()
        if r < 0.002:
            t += rng.randint(1, 4) * 1_000_000_000            # everyone idle
        elif r < 0.01:
            t += rng.randint(1, 900_000_000)
        else:
            t += rng.choice([0, 0, 1, 50, 900, 4_000, 4_999, 5_000, 6_000, 30_000])
        channel = rng.choice([68, 68, 70, 71])
        if r > 0.999:
            packetizer.bump_version(t, channel, final_message=b"\x0c\x00" + bytes(10),
                                    final_tag=("reset", step))
        elif 0.998 < r <= 0.999:
            up = t + rng.randint(1, 3) * 1_000_000_000
            packetizer.stream_down(t, channel, up)
            t = up
        else:
            size = rng.choice([20, 60, 200, 700, 1300])
            packetizer.add(t, channel, size.to_bytes(2, "little") + bytes(size - 2),
                           tag=step)
    return t


@pytest.mark.parametrize("seed", range(30))
def test_sink_order_equals_finish_order(seed):
    reference = Packetizer([68, 70, 71], 0, window_ns=5000)
    end = drive(reference, seed)
    want = reference.finish(end + 5_000_000_000)

    got = []
    streaming = Packetizer([68, 70, 71], 0, window_ns=5000,
                           sink=lambda *packet: got.append(packet))
    drive(streaming, seed)
    tail = streaming.finish(end + 5_000_000_000)

    assert [(t, c, data) for _, t, c, _, _, _, data in got] == want.packets
    assert [position for position, *_ in got] == list(range(len(want.packets)))
    placements = {}
    for position, t, c, version, seq, contents, _ in got:
        if seq:
            for index, tag in enumerate(contents):
                placements[tag] = (c, version, seq, index, t, position)
    assert placements == {tag: (p.channel, p.sequence_version, p.sequence_number, p.index,
                                p.sending_time, p.packet_position)
                          for tag, p in want.placements.items()}
    assert tail.heartbeats == want.heartbeats and want.heartbeats


def test_streaming_rejects_time_going_back_across_channels():
    p = Packetizer([68, 70], 0, sink=lambda *a: None)
    p.add(100, 68, b"\x0c\x00" + bytes(10))
    with pytest.raises(ValueError, match="streaming"):
        p.add(50, 70, b"\x0c\x00" + bytes(10))


def test_writer_forgets_messages_it_has_written(tmp_path):
    config = main.default_config(seed=1, target_messages=3000, out_dir=tmp_path)
    published = {}
    original = golden.StreamWriter

    class Spy(original):
        def __init__(self, out_dir, items, *args):
            published["items"] = items
            super().__init__(out_dir, items, *args)

    golden.StreamWriter = Spy
    try:
        main.run(config)
    finally:
        golden.StreamWriter = original
    assert published["items"] == {}


def test_an_injection_past_the_end_is_an_error(tmp_path):
    config = main.default_config(seed=1, target_messages=500, out_dir=tmp_path)
    late = scenarios.Inject("trade_bust", 10_000, (("instrument", "WIN"),))
    with pytest.raises(scenarios.ScenarioError, match="never placed.*raise it"):
        main.run(dataclasses.replace(config, injections=(late,)))
