# Shared cocotb settings. Each tb/unit/<module>/Makefile sets
# COCOTB_TOPLEVEL, COCOTB_TEST_MODULES, VERILOG_SOURCES, then includes this.

# ---- 1. Paths ----
RTL_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/../../rtl)

# ---- 2. Simulator ----
SIM           ?= verilator
TOPLEVEL_LANG ?= verilog

# ---- 3. cocotb rules ----
include $(shell cocotb-config --makefiles)/Makefile.sim
