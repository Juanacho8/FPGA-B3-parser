# Project entry point. Targets are added phase by phase.

# ---- 1. Use the project venv's tools (cocotb-config, python) ----
export PATH := $(CURDIR)/.venv/bin:$(PATH)

# ---- 2. Unit tests: one directory per module under tb/unit ----
UNIT_DIR   := rtl_parser/tb/unit
UNIT_TESTS := $(notdir $(patsubst %/,%,$(dir $(wildcard $(UNIT_DIR)/*/Makefile))))

.PHONY: sim-all clean data layouts layouts-check pcap-crosscheck cpp-release cpp-debug cpp-test cpp-k16 cpp-k32 cpp-uncapped cpp-variants cpp-clean

sim-%:
	$(MAKE) -C $(UNIT_DIR)/$*

sim-all: $(addprefix sim-,$(UNIT_TESTS))

# ---- 3. Test data: every generator set, into data/ (gitignored) ----
PYTHON  ?= python3
B3_DATA ?= data

data:
	cd feed_generator && $(PYTHON) -m b3gen build --out $(abspath $(B3_DATA))

# ---- 4. Generated layouts: one script, one parse of B3's XML, both outputs ----
LAYOUT_FILES := feed_generator/b3gen/layouts.py cpp_baseline/include/b3/layouts.hpp

layouts:
	$(PYTHON) tools/gen_layouts.py

# Regenerate into a temp folder; fail if a checked-in file differs.
layouts-check:
	@tmp=$$(mktemp -d) && trap 'rm -rf "$$tmp"' EXIT && \
	$(PYTHON) tools/gen_layouts.py --out-dir "$$tmp" > /dev/null && \
	status=0 && \
	for f in $(LAYOUT_FILES); do diff -u "$$f" "$$tmp/$$f" || status=1; done && \
	if [ $$status -ne 0 ]; then echo "layouts-check: generated files are stale or hand-edited; run 'make layouts'" >&2; exit 1; fi && \
	echo "layouts-check: $(LAYOUT_FILES) up to date"

# ---- 5. C++ baseline: one build folder per configuration under cpp_baseline/build ----
CPP_DIR   := cpp_baseline
CPP_BUILD := $(CPP_DIR)/build

# $(call cpp_build,<folder>,<cmake args>): configure and compile one variant
define cpp_build
	cmake -S $(CPP_DIR) -B $(CPP_BUILD)/$(1) $(2)
	cmake --build $(CPP_BUILD)/$(1) --parallel
endef

cpp-release:
	$(call cpp_build,release,-DCMAKE_BUILD_TYPE=Release)

cpp-debug:
	$(call cpp_build,debug,-DCMAKE_BUILD_TYPE=Debug)

cpp-test: layouts-check cpp-debug
	ctest --test-dir $(CPP_BUILD)/debug --output-on-failure

cpp-k16:
	$(call cpp_build,k16,-DCMAKE_BUILD_TYPE=Release -DB3_TRACKED_DEPTH=16)

cpp-k32:
	$(call cpp_build,k32,-DCMAKE_BUILD_TYPE=Release -DB3_TRACKED_DEPTH=32)

cpp-uncapped:
	$(call cpp_build,uncapped,-DCMAKE_BUILD_TYPE=Release -DB3_BOOK=uncapped)

cpp-variants: cpp-k16 cpp-k32 cpp-uncapped

# The C++ pcap reader against the generator's independent Python reader, on
# feedA.pcap and feedB.pcap of every set under B3_DATA (make data). Fails on any difference.

pcap-crosscheck: cpp-release
	$(PYTHON) tools/pcap_crosscheck.py --cpp $(CPP_BUILD)/release/b3_pcapinfo $(B3_DATA)

cpp-clean:
	rm -rf $(CPP_BUILD)

# ---- 6. Clean ----
clean: cpp-clean
	for t in $(UNIT_TESTS); do $(MAKE) -C $(UNIT_DIR)/$$t clean; rm -f $(UNIT_DIR)/$$t/results.xml; done
