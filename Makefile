# Project entry point. Targets are added phase by phase.

# ---- 1. Use the project venv's tools (cocotb-config, python) ----
export PATH := $(CURDIR)/.venv/bin:$(PATH)

# ---- 2. Unit tests: one directory per module under tb/unit ----
UNIT_DIR   := rtl_parser/tb/unit
UNIT_TESTS := $(notdir $(patsubst %/,%,$(dir $(wildcard $(UNIT_DIR)/*/Makefile))))

.PHONY: sim-all clean data layouts layouts-check pcap-crosscheck decode-crosscheck decode-compare counters-compare events-compare book-compare book-oracle mbo-check goldens cpp-release cpp-debug cpp-debug-k4 cpp-test cpp-k16 cpp-k32 cpp-uncapped cpp-n2k3 cpp-uncapped-n2 cpp-variants cpp-book-variants cpp-clean

sim-%:
	$(MAKE) -C $(UNIT_DIR)/$*

sim-all: $(addprefix sim-,$(UNIT_TESTS))

# ---- 3. Test data: every generator set, into data/ (gitignored) ----
PYTHON  ?= python3
B3_DATA ?= data

data:
	cd feed_generator && $(PYTHON) -m b3gen build --out $(abspath $(B3_DATA))

# ---- 4. Generated layouts: one script, one parse of B3's XML, both outputs ----
LAYOUT_FILES := feed_generator/b3gen/layouts.py cpp_baseline/include/b3/layouts.hpp cpp_baseline/include/b3/fields.hpp

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

# Debug (sanitizers) at K = 4, N = 2: the capped book's truncation paths
# with the build's own parameters as well as the tests' explicit K = 4.
cpp-debug-k4:
	$(call cpp_build,debug-k4,-DCMAKE_BUILD_TYPE=Debug -DB3_PUBLISHED_DEPTH=2 -DB3_TRACKED_DEPTH=4)

cpp-test: layouts-check cpp-debug cpp-debug-k4
	ctest --test-dir $(CPP_BUILD)/debug --output-on-failure
	ctest --test-dir $(CPP_BUILD)/debug-k4 --output-on-failure

cpp-k16:
	$(call cpp_build,k16,-DCMAKE_BUILD_TYPE=Release -DB3_TRACKED_DEPTH=16)

cpp-k32:
	$(call cpp_build,k32,-DCMAKE_BUILD_TYPE=Release -DB3_TRACKED_DEPTH=32)

cpp-uncapped:
	$(call cpp_build,uncapped,-DCMAKE_BUILD_TYPE=Release -DB3_BOOK=uncapped)

cpp-variants: cpp-k16 cpp-k32 cpp-uncapped

# The generator's books are thin (K = 16 never truncates on them), so a
# small capped book, N = 2 and K = 3, runs truncation on real order flow,
# with an uncapped N = 2 build as its oracle.
cpp-n2k3:
	$(call cpp_build,n2k3,-DCMAKE_BUILD_TYPE=Release -DB3_PUBLISHED_DEPTH=2 -DB3_TRACKED_DEPTH=3)

cpp-uncapped-n2:
	$(call cpp_build,uncapped-n2,-DCMAKE_BUILD_TYPE=Release -DB3_BOOK=uncapped -DB3_PUBLISHED_DEPTH=2)

cpp-book-variants: cpp-variants cpp-n2k3 cpp-uncapped-n2

# The C++ pcap reader against the generator's independent Python reader, on
# feedA.pcap and feedB.pcap of every set under B3_DATA (make data). Fails on any difference.

pcap-crosscheck: cpp-release
	$(PYTHON) tools/pcap_crosscheck.py --cpp $(CPP_BUILD)/release/b3_pcapinfo $(B3_DATA)

# The packet decoder and message walker (b3_decode) against intent.jsonl and
# manifest.json on both feeds of every set under B3_DATA. Fails on any difference.
decode-crosscheck: cpp-release
	$(PYTHON) tools/decode_crosscheck.py --cpp $(CPP_BUILD)/release/b3_decode $(B3_DATA)

# The full decode (b3_decode --decode) against intent.jsonl in compare_decode
# --strict mode, both feeds of every set under B3_DATA, plus the schema set's
# features and B3's sample packet. Fails on any difference.
decode-compare: cpp-release
	$(PYTHON) tools/decode_compare.py --cpp $(CPP_BUILD)/release/b3_decode $(B3_DATA)

# Sequencing, rptSeq, stale flags and every other counter of b3_decode
# --counters against values derived from manifest.json and intent.jsonl,
# both feeds of every set under B3_DATA. Fails on any difference.
counters-compare: cpp-release
	$(PYTHON) tools/counters_compare.py --cpp $(CPP_BUILD)/release/b3_decode $(B3_DATA)

# Port 1: b3_decode --events against tools/intent_to_events.py (Feed A byte
# for byte, Feed B per channel) in the release and variant builds; records
# must be identical across builds. Fails on any difference.
EVENT_BUILDS := $(foreach b,release k16 k32 uncapped,--build $(b):$(CPP_BUILD)/$(b)/b3_decode)

events-compare: cpp-release cpp-variants
	$(PYTHON) tools/events_compare.py $(B3_DATA) $(EVENT_BUILDS)

# Port 2: b3_decode --book of each book build (uncapped, capped K = 16 and
# K = 32 at N = 5, and the N = 2 pair) against tools/expected_book.py with the
# same N and K (byte for byte, both feeds), the book key at every checkpoint
# for CLEAN instruments (sides not depth incomplete), and the book counters.
# Fails on any difference.
BOOK_BUILDS := uncapped k16 k32 n2k3 uncapped-n2

book-compare: cpp-book-variants
	set -e; for b in $(BOOK_BUILDS); do \
	  echo "== book-compare: $$b"; \
	  $(PYTHON) tools/book_compare.py --cpp $(CPP_BUILD)/$$b/b3_decode $(B3_DATA); \
	done

# The capped book against the uncapped book with the same N, every set and
# feed: identical records except K, the depth-incomplete flags and the levels
# of flagged sides; reports truncation and depth-incomplete statistics per
# (N, K). Fails on any other difference.
BOOK_ORACLE_PAIRS := k16:uncapped k32:uncapped n2k3:uncapped-n2

book-oracle: cpp-book-variants
	set -e; for pair in $(BOOK_ORACLE_PAIRS); do \
	  capped=$${pair%%:*}; uncapped=$${pair##*:}; \
	  echo "== book-oracle: $$capped against $$uncapped"; \
	  $(PYTHON) tools/book_oracle.py --capped $(CPP_BUILD)/$$capped/b3_decode \
	    --uncapped $(CPP_BUILD)/$$uncapped/b3_decode $(B3_DATA); \
	done

# The MBO book (b3_decode --mbo, uncapped build, step 9) on both feeds of
# every set: 0 per-order faults while CLEAN, 0 level mismatches against the
# uncapped book after every event close, and the book key at every CLEAN
# checkpoint. Fails on any difference.
mbo-check: cpp-uncapped
	$(PYTHON) tools/mbo_check.py --cpp $(CPP_BUILD)/uncapped/b3_decode $(B3_DATA)

# Goldens: each set's expected and C++ events.bin, and capped book.bin for
# K = 16 and K = 32 at N = 5, under $(B3_DATA)/golden/, with provenance.json.
# Reproducible from make data && make goldens.
goldens: cpp-release cpp-k16 cpp-k32
	$(PYTHON) tools/make_goldens.py --cpp $(CPP_BUILD)/release/b3_decode \
	  --book-cpp $(CPP_BUILD)/k16/b3_decode --book-cpp $(CPP_BUILD)/k32/b3_decode $(B3_DATA)

cpp-clean:
	rm -rf $(CPP_BUILD)

# ---- 6. Clean ----
clean: cpp-clean
	for t in $(UNIT_TESTS); do $(MAKE) -C $(UNIT_DIR)/$$t clean; rm -f $(UNIT_DIR)/$$t/results.xml; done
