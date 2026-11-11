# Unit test for msg_splitter.
#
# 1. B3's Message Reference section 6 sample: exact beats out, incl. the
#    realigned message 1 (it starts at input beat 12, lane 4)
# 2. Hand-made faults the generator doesn't produce:
#    blockLength > messageLength - 12, and a message cut exactly at a beat edge
# 3. Every packet of every generated data set, with random stalls on both
#    sides, against reference_split() below. For `malformed`, the number of
#    good messages per faulty packet must also equal the manifest's
#    messages_before (an independent check from the generator).
#
# reference_split() is test equipment: it applies docs/DESIGN.md section 4
# with messageLength, the same way a software reader would.

import json
import os
import struct
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

import axis
import pcap

DATA_DIR = Path(os.environ["DATA_DIR"])
SAMPLE_HEX = Path(os.environ["SAMPLE_HEX"])
SIDEBAND = ("m_channel", "m_seq_version", "m_seq_num", "m_msg_index", "m_msg_ok")


# ---- 1. Reference: what the splitter must output for one packet ----
def reference_split(payload):
    """List of (channel, seq_version, seq_num, msg_index, message bytes, ok)."""
    if len(payload) < 16:
        return []
    channel, _, seq_version, seq_num = struct.unpack_from("<BBHI", payload, 0)
    out = []
    pos, index = 16, 0
    while pos < len(payload):
        if len(payload) - pos < 6:
            break                                   # header cut before blockLength
        length, encoding, block = struct.unpack_from("<HHH", payload, pos)
        if length < 12 or encoding != 0xEB50 or block > length - 12:
            break                                   # drop rest of packet
        end = pos + length
        if end > len(payload):
            out.append((channel, seq_version, seq_num, index, payload[pos:], False))
            break                                   # packet ended inside the message
        out.append((channel, seq_version, seq_num, index, payload[pos:end], True))
        pos, index = end, index + 1
    return out


# ---- 2. Rebuild messages from the sink's beats ----
def messages_from_beats(beats):
    out, data = [], b""
    for tdata, tkeep, tlast, side in beats:
        n = bin(tkeep).count("1")
        assert tkeep == (1 << n) - 1, f"tkeep not contiguous: {tkeep:#x}"
        data += tdata.to_bytes(8, "little")[:n]
        if tlast:
            out.append((side["m_channel"], side["m_seq_version"], side["m_seq_num"],
                        side["m_msg_index"], data, bool(side["m_msg_ok"])))
            data = b""
    return out


# ---- 3. Bench: clock, reset, source, sink ----
async def start(dut, stall=0.0):
    cocotb.start_soon(Clock(dut.clk, 6400, unit="ps").start())   # 156.25 MHz
    source = axis.AxisSource(dut, dut.clk, "s_axis", stall=stall)
    sink = axis.AxisSink(dut, dut.clk, "m_axis", sideband=SIDEBAND, stall=stall)
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    cocotb.start_soon(sink.run())
    return source, sink


async def drain(dut):
    await ClockCycles(dut.clk, 50)


def sample_packet():
    lines = [l for l in SAMPLE_HEX.read_text().splitlines() if l and not l.startswith("#")]
    return bytes.fromhex(" ".join(lines))


@cocotb.test()
async def test_sample_packet(dut):
    source, sink = await start(dut)
    packet = sample_packet()
    await source.send(packet)
    await drain(dut)

    # 3a. Two complete messages, exactly the reference
    got = messages_from_beats(sink.beats)
    assert got == reference_split(packet)
    assert [len(m[4]) for m in got] == [84, 68]

    # 3b. Beat level: message 0 = 11 beats (last has 4 bytes), message 1 = 9 beats
    keeps = [b[1] for b in sink.beats]
    assert keeps == [0xFF] * 10 + [0x0F] + [0xFF] * 8 + [0x0F]

    # 3c. Message 1 realigned: its first out beat = packet bytes 0x64..0x6b
    first_of_msg1 = sink.beats[11][0].to_bytes(8, "little")
    assert first_of_msg1 == packet[0x64:0x6C]
    assert first_of_msg1[:2] == bytes.fromhex("4400")               # messageLength 68


@cocotb.test()
async def test_handmade_faults(dut):
    source, sink = await start(dut)
    packet = bytearray(sample_packet())

    # 4a. blockLength 57 > 68 - 12 in message 1: message 0 kept, rest dropped
    bad_block = bytearray(packet)
    struct.pack_into("<H", bad_block, 0x64 + 4, 57)
    await source.send(bytes(bad_block))

    # 4b. Message 0 claims 200 bytes; the packet ends after 152 = 19 full beats
    past_end = bytearray(packet)
    struct.pack_into("<H", past_end, 0x10, 200)
    struct.pack_into("<I", past_end, 4, 987654322)                   # next seq_num
    await source.send(bytes(past_end))
    await drain(dut)

    got = messages_from_beats(sink.beats)
    expected = reference_split(bytes(bad_block)) + reference_split(bytes(past_end))
    assert got == expected
    assert [m[5] for m in got] == [True, False]
    assert sink.beats[-1][1] == 0x00                                 # 0-byte closing beat


@cocotb.test()
async def test_generated_feeds(dut):
    source, sink = await start(dut, stall=0.2)
    sets = sorted(DATA_DIR.glob("small")) + sorted(DATA_DIR.glob("scenarios/*"))
    assert sets, f"no generated data in {DATA_DIR}: run `make gen-data`"

    for folder in sets:
        sink.beats.clear()
        packets = list(pcap.payloads(folder / "feedA.pcap"))
        for packet in packets:
            await source.send(packet)
        await drain(dut)

        # 5a. Every message, byte for byte, with labels and ok flag
        expected = [m for p in packets for m in reference_split(p)]
        got = messages_from_beats(sink.beats)
        assert len(got) == len(expected), f"{folder.name}: {len(got)} vs {len(expected)} messages"
        for g, e in zip(got, expected):
            assert g == e, f"{folder.name}: first mismatch at {e[:4]}"

        # 5b. malformed: good messages per faulty packet == manifest messages_before
        manifest = json.loads((folder / "manifest.json").read_text())
        for fault in manifest.get("faults") or []:
            if fault["fault"] == "short":
                continue
            key = (fault["channel"], fault["sequenceVersion"], fault["sequenceNumber"])
            good = sum(1 for m in got if m[:3] == key and m[5])
            assert good == fault["messages_before"], f"{fault}: {good} good messages"

        dut._log.info(f"{folder.name}: {len(packets)} packets, {len(got)} messages OK")
