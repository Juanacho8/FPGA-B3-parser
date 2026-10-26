# Project entry point. Targets are added phase by phase.

# ---- 1. Use the project venv's tools (cocotb-config, python) ----
export PATH := $(CURDIR)/.venv/bin:$(PATH)

# ---- 2. Unit tests: one directory per module under tb/unit ----
UNIT_DIR   := rtl_parser/tb/unit
UNIT_TESTS := $(notdir $(patsubst %/,%,$(dir $(wildcard $(UNIT_DIR)/*/Makefile))))

.PHONY: sim-all clean cpp-release cpp-debug cpp-test cpp-k16 cpp-k32 cpp-uncapped cpp-variants cpp-clean

sim-%:
	$(MAKE) -C $(UNIT_DIR)/$*

sim-all: $(addprefix sim-,$(UNIT_TESTS))

# ---- 3. C++ baseline: one build folder per configuration under cpp_baseline/build ----
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

cpp-test: cpp-debug
	ctest --test-dir $(CPP_BUILD)/debug --output-on-failure

cpp-k16:
	$(call cpp_build,k16,-DCMAKE_BUILD_TYPE=Release -DB3_TRACKED_DEPTH=16)

cpp-k32:
	$(call cpp_build,k32,-DCMAKE_BUILD_TYPE=Release -DB3_TRACKED_DEPTH=32)

cpp-uncapped:
	$(call cpp_build,uncapped,-DCMAKE_BUILD_TYPE=Release -DB3_BOOK=uncapped)

cpp-variants: cpp-k16 cpp-k32 cpp-uncapped

cpp-clean:
	rm -rf $(CPP_BUILD)

# ---- 4. Clean ----
clean: cpp-clean
	for t in $(UNIT_TESTS); do $(MAKE) -C $(UNIT_DIR)/$$t clean; rm -f $(UNIT_DIR)/$$t/results.xml; done
