# Project entry point. Targets are added phase by phase.

# ---- 1. Use the project venv's tools (cocotb-config, python) ----
export PATH := $(CURDIR)/.venv/bin:$(PATH)

# ---- 2. Unit tests: one directory per module under tb/unit ----
UNIT_DIR   := rtl_parser/tb/unit
UNIT_TESTS := $(notdir $(patsubst %/,%,$(dir $(wildcard $(UNIT_DIR)/*/Makefile))))

.PHONY: sim-all clean

sim-%:
	$(MAKE) -C $(UNIT_DIR)/$*

sim-all: $(addprefix sim-,$(UNIT_TESTS))

# ---- 3. Clean ----
clean:
	for t in $(UNIT_TESTS); do $(MAKE) -C $(UNIT_DIR)/$$t clean; rm -f $(UNIT_DIR)/$$t/results.xml; done
