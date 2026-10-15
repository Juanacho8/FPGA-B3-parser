# Unit test for reset_sync.
#
# 1. Held in reset while arst_n = 0
# 2. Release arst_n between edges -> rst_n rises exactly STAGES edges later
# 3. Assert arst_n between edges -> rst_n falls immediately, no clock edge

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer, ReadOnly

STAGES = 2


@cocotb.test()
async def test_assert_async_release_sync(dut):
    # ---- Setup: 10 ns clock, start in reset ----
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.arst_n.value = 0

    # ---- 1. Held in reset ----
    for _ in range(3):
        await RisingEdge(dut.clk)
    await ReadOnly()
    assert dut.rst_n.value == 0, "rst_n must stay 0 while arst_n is 0"

    # ---- 2. Release between edges, count edges until rst_n = 1 ----
    await Timer(4, unit="ns")
    dut.arst_n.value = 1

    edges = 0
    while True:
        await RisingEdge(dut.clk)
        edges += 1
        await ReadOnly()
        if dut.rst_n.value == 1:
            break
        assert edges < 10, "rst_n never released"
    assert edges == STAGES, f"released after {edges} edges, expected {STAGES}"

    # ---- 3. Assert between edges: rst_n drops with no clock edge ----
    await Timer(3, unit="ns")
    dut.arst_n.value = 0
    await Timer(1, unit="ns")
    assert dut.rst_n.value == 0, "rst_n must assert asynchronously"
