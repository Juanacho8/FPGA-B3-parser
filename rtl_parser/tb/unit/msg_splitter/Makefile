COCOTB_TOPLEVEL     = msg_splitter
COCOTB_TEST_MODULES = test_msg_splitter
VERILOG_SOURCES     = $(RTL_DIR)/pkg/pkg_types.sv $(RTL_DIR)/parser/msg_splitter.sv

include ../common.mk

# Generated feeds (make gen-data) and B3's Message Reference section 6 sample
REPO_ROOT := $(abspath $(RTL_DIR)/../..)
export DATA_DIR   := $(REPO_ROOT)/data
export SAMPLE_HEX := $(REPO_ROOT)/feed_generator/tests/fixtures/b3_sample_packet/packet.hex
