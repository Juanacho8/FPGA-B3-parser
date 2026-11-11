# AXI-Stream helpers for cocotb tests (64-bit tdata, byte 0 = tdata[7:0]).
#
# 1. to_beats(payload): bytes -> list of (tdata, tkeep, tlast)
# 2. AxisSource: drives beats, holds each one until tready
# 3. AxisSink: collects beats; optional random tready stalls
#
# Handshakes are sampled in ReadOnly (just before the clock edge), so values
# are final for the cycle.

import random

from cocotb.triggers import ReadOnly, RisingEdge


# ---- 1. Bytes -> beats ----
def to_beats(payload):
    beats = []
    for i in range(0, len(payload), 8):
        chunk = payload[i : i + 8]
        tdata = int.from_bytes(chunk.ljust(8, b"\x00"), "little")
        tkeep = (1 << len(chunk)) - 1
        tlast = i + 8 >= len(payload)
        beats.append((tdata, tkeep, int(tlast)))
    return beats


# ---- 2. Source ----
class AxisSource:
    def __init__(self, dut, clk, prefix, stall=0.0, seed=1):
        self.clk = clk
        self.tdata = getattr(dut, f"{prefix}_tdata")
        self.tkeep = getattr(dut, f"{prefix}_tkeep")
        self.tvalid = getattr(dut, f"{prefix}_tvalid")
        self.tready = getattr(dut, f"{prefix}_tready")
        self.tlast = getattr(dut, f"{prefix}_tlast")
        self.stall = stall
        self.rng = random.Random(seed)
        self.tvalid.value = 0

    async def send(self, payload):
        for tdata, tkeep, tlast in to_beats(payload):
            # 2a. Random idle cycles before the beat
            while self.rng.random() < self.stall:
                self.tvalid.value = 0
                await RisingEdge(self.clk)

            # 2b. Offer the beat until the DUT takes it
            self.tdata.value = tdata
            self.tkeep.value = tkeep
            self.tlast.value = tlast
            self.tvalid.value = 1
            while True:
                await ReadOnly()
                taken = self.tready.value == 1
                await RisingEdge(self.clk)
                if taken:
                    break
        self.tvalid.value = 0


# ---- 3. Sink ----
class AxisSink:
    """Collects beats as (tdata, tkeep, tlast, {sideband name: value})."""

    def __init__(self, dut, clk, prefix, sideband=(), stall=0.0, seed=2):
        self.clk = clk
        self.dut = dut
        self.prefix = prefix
        self.sideband = sideband
        self.stall = stall
        self.rng = random.Random(seed)
        self.beats = []

    def _sig(self, name):
        return getattr(self.dut, f"{self.prefix}_{name}")

    async def run(self):
        tready = self._sig("tready")
        while True:
            ready = self.rng.random() >= self.stall
            tready.value = int(ready)
            await ReadOnly()
            if ready and self._sig("tvalid").value == 1:
                side = {n: int(getattr(self.dut, n).value) for n in self.sideband}
                self.beats.append((
                    int(self._sig("tdata").value),
                    int(self._sig("tkeep").value),
                    int(self._sig("tlast").value),
                    side,
                ))
            await RisingEdge(self.clk)
