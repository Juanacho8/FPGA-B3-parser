# Project entry point. Targets are added phase by phase.

# ---- 1. Use the project venv's tools (cocotb-config, python) ----
export PATH := $(CURDIR)/.venv/bin:$(PATH)

# ---- 2. Unit tests: one directory per module under tb/unit ----
UNIT_DIR   := rtl_parser/tb/unit
UNIT_TESTS := $(notdir $(patsubst %/,%,$(dir $(wildcard $(UNIT_DIR)/*/Makefile))))

.PHONY: sim-all clean data layouts layouts-check field-audit field-audit-check pcap-crosscheck decode-crosscheck decode-compare counters-compare events-compare book-compare book-oracle mbo-check fuzz check-fast cpp-timed cpp-opt bench cpp-profile profile goldens cpp-release cpp-debug cpp-debug-k4 cpp-debug-opt cpp-test cpp-k16 cpp-k32 cpp-uncapped cpp-n2k3 cpp-uncapped-n2 cpp-variants cpp-book-variants cpp-clean

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

# The field audit (DESIGN.md 3.10, cpp_baseline/include/b3/field_audit.hpp),
# generated from tools/field_audit.py's rules and the parsed schema.
field-audit:
	$(PYTHON) tools/field_audit.py

field-audit-check:
	$(PYTHON) tools/field_audit.py --check

# Debug (sanitizers) of the optimised implementation: every unit test, the
# capped book's K = 4 truncation tests included, runs on its code paths too.
cpp-debug-opt:
	$(call cpp_build,debug-opt,-DCMAKE_BUILD_TYPE=Debug -DB3_IMPL=optimised)

cpp-test: layouts-check field-audit-check cpp-debug cpp-debug-k4 cpp-debug-opt
	ctest --test-dir $(CPP_BUILD)/debug --output-on-failure
	ctest --test-dir $(CPP_BUILD)/debug-k4 --output-on-failure
	ctest --test-dir $(CPP_BUILD)/debug-opt --output-on-failure

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
EVENT_BUILDS := $(foreach b,release k16 k32 uncapped opt-k16 opt-k32 opt-uncapped,--build $(b):$(CPP_BUILD)/$(b)/b3_decode)

events-compare: cpp-release cpp-variants cpp-opt
	$(PYTHON) tools/events_compare.py $(B3_DATA) $(EVENT_BUILDS)

# Port 2: b3_decode --book of each book build (uncapped, capped K = 16 and
# K = 32 at N = 5, and the N = 2 pair) against tools/expected_book.py with the
# same N and K (byte for byte, both feeds), the book key at every checkpoint
# for CLEAN instruments (sides not depth incomplete), and the book counters.
# Fails on any difference.
BOOK_BUILDS := uncapped k16 k32 n2k3 uncapped-n2 opt-k16 opt-k32 opt-uncapped opt-n2k3

book-compare: cpp-book-variants cpp-opt
	set -e; for b in $(BOOK_BUILDS); do \
	  echo "== book-compare: $$b"; \
	  $(PYTHON) tools/book_compare.py --cpp $(CPP_BUILD)/$$b/b3_decode $(B3_DATA); \
	done

# The capped book against the uncapped book with the same N, every set and
# feed: identical records except K, the depth-incomplete flags and the levels
# of flagged sides; reports truncation and depth-incomplete statistics per
# (N, K). Fails on any other difference.
BOOK_ORACLE_PAIRS := k16:uncapped k32:uncapped n2k3:uncapped-n2 opt-n2k3:uncapped-n2

book-oracle: cpp-book-variants cpp-opt
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

# Timed builds (brief section 8, step 10): Release with B3_TIMED=ON, the
# diagnostic counters compiled out; outputs unchanged.
cpp-timed:
	$(call cpp_build,timed-k16,-DCMAKE_BUILD_TYPE=Release -DB3_TIMED=ON -DB3_TRACKED_DEPTH=16)
	$(call cpp_build,timed-k32,-DCMAKE_BUILD_TYPE=Release -DB3_TIMED=ON -DB3_TRACKED_DEPTH=32)
	$(call cpp_build,timed-uncapped,-DCMAKE_BUILD_TYPE=Release -DB3_TIMED=ON -DB3_BOOK=uncapped)

# Optimised builds (step 10b, B3_IMPL=optimised, docs/bench/optimise.md): the
# timing baseline, timed, byte-identical to the goldens (events-compare,
# book-compare and book-oracle run them).
OPT_BASE := -DCMAKE_BUILD_TYPE=Release -DB3_TIMED=ON -DB3_IMPL=optimised

cpp-opt:
	$(call cpp_build,opt-k16,$(OPT_BASE) -DB3_TRACKED_DEPTH=16)
	$(call cpp_build,opt-k32,$(OPT_BASE) -DB3_TRACKED_DEPTH=32)
	$(call cpp_build,opt-uncapped,$(OPT_BASE) -DB3_BOOK=uncapped)
	$(call cpp_build,opt-n2k3,$(OPT_BASE) -DB3_PUBLISHED_DEPTH=2 -DB3_TRACKED_DEPTH=3)

# The benchmark (apps/b3_bench.cpp via tools/bench.py): each timed build on
# large (throughput) and burst (tail latency), Feed A, after its golden check.
# Run on AC power, lid open, nothing heavy running. BENCH_PASSES, BENCH_OUT.
BENCH_PASSES ?= 30
BENCH_OUT ?= docs/bench/results
BENCH_BUILDS := $(foreach b,k16 k32 uncapped,--build $(b):$(CPP_BUILD)/timed-$(b)/b3_bench) \
                $(foreach b,k16 k32 uncapped,--build opt-$(b):$(CPP_BUILD)/opt-$(b)/b3_bench)

bench: cpp-timed cpp-opt
	$(PYTHON) tools/bench.py $(BENCH_BUILDS) --passes $(BENCH_PASSES) --out $(BENCH_OUT) $(B3_DATA)

# The profile (tools/profile.py): the capped K = 16 timed build with every
# stage's entry point out of line (B3_PROFILE=ON), sampled by /usr/bin/sample
# on large during timed passes; per-stage shares, not times.
cpp-profile:
	$(call cpp_build,profile-k16,-DCMAKE_BUILD_TYPE=Release -DB3_TIMED=ON -DB3_PROFILE=ON -DB3_TRACKED_DEPTH=16)

profile: cpp-profile
	$(PYTHON) tools/profile.py --bench $(CPP_BUILD)/profile-k16/b3_bench \
	  --golden-events $(B3_DATA)/golden/large/expected_feedA.bin \
	  --golden-book $(B3_DATA)/golden/large/expected_book_n5_k16_feedA.bin --out docs/bench/profile_k16_large.md \
	  $(B3_DATA)/large/feedA.pcap

# The quick loop: every build, the unit tests (C++ under sanitizers, and the
# tools'), and every compare target on the small set and the scenarios only:
# no large or burst set, no goldens, no fuzz. The full run (every target on
# every set, goldens, fuzz) stays the gate before every commit. The sets are
# cloned into $(FAST_DATA) (copy-on-write on APFS: the tools' rglob does not
# follow symlinked folders).
FAST_DATA := $(CPP_BUILD)/fast_data
FAST_TARGETS := field-audit-check pcap-crosscheck decode-crosscheck decode-compare counters-compare \
                events-compare mbo-check book-compare book-oracle

check-fast: cpp-test cpp-release cpp-book-variants
	rm -rf $(FAST_DATA) && mkdir -p $(FAST_DATA)
	for s in small scenarios; do cp -cR $(B3_DATA)/$$s $(FAST_DATA)/ 2>/dev/null || cp -R $(B3_DATA)/$$s $(FAST_DATA)/; done
	set -e; for t in $(FAST_TARGETS); do echo "== $$t"; $(MAKE) --no-print-directory $$t B3_DATA=$(FAST_DATA); done
	$(PYTHON) -m pytest -q tools/tests

# Damaged packets from the small set's feedA.pcap through the whole pipeline
# in the Debug (sanitizer) build: any crash or undefined behaviour aborts with
# the iteration and mutation; tools/fuzz_report.py prints the outcomes per
# mutation kind. FUZZ_ITERATIONS and FUZZ_SEED override the defaults.
FUZZ_ITERATIONS ?= 1600
FUZZ_SEED ?= 1

fuzz: cpp-debug
	$(PYTHON) tools/fuzz_report.py --fuzz $(CPP_BUILD)/debug/b3_fuzz --iterations $(FUZZ_ITERATIONS) \
	  --seed $(FUZZ_SEED) $(B3_DATA)/small/feedA.pcap

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
