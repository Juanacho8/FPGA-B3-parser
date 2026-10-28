# C++ baseline

Software feed handler for B3 Binary UMDF: pcap in, event records (port 1) and
book updates (port 2) out. It is the correctness reference, the RTL's golden
model, and the software timing baseline. Full brief: `docs/CPP_BASELINE_CONTEXT.md`.

Status: step 9 (build system, generated layouts, pcap reader, packet decoder, message walker, field decoding, full decode writer, sequencing, instrument table, rptSeq and stale flags, port 1 event records, the uncapped book and port 2, the capped book, the MBO book).

## Requirements

CMake 3.20 or newer and a C++20 compiler (Apple clang 17 tested).
On macOS: `brew install cmake`.

## Build and test

From the repo root:

| Target | What it does | Build folder |
|---|---|---|
| `make cpp-release` | `-O3 -mcpu=native` | `cpp_baseline/build/release` |
| `make cpp-debug` | `-O1 -g`, AddressSanitizer + UndefinedBehaviorSanitizer | `cpp_baseline/build/debug` |
| `make cpp-debug-k4` | Debug (sanitizers), N = 2, K = 4 | `cpp_baseline/build/debug-k4` |
| `make cpp-test` | `make layouts-check`, the Debug and Debug K = 4 builds, then runs the tests with `ctest` in both | `cpp_baseline/build/debug`, `debug-k4` |
| `make cpp-k16` | Release, K = 16 | `cpp_baseline/build/k16` |
| `make cpp-k32` | Release, K = 32 | `cpp_baseline/build/k32` |
| `make cpp-uncapped` | Release, uncapped book | `cpp_baseline/build/uncapped` |
| `make cpp-variants` | The three variants above | |
| `make cpp-n2k3` | Release, N = 2, K = 3: truncation on the generator's thin books | `cpp_baseline/build/n2k3` |
| `make cpp-uncapped-n2` | Release, uncapped, N = 2: the oracle for n2k3 | `cpp_baseline/build/uncapped-n2` |
| `make cpp-book-variants` | `cpp-variants` plus the two N = 2 builds | |
| `make cpp-clean` | Deletes `cpp_baseline/build` | |
| `make data` | Runs the feed generator for every set into `data/` (gitignored), the default `B3_DATA` | |
| `make events-compare` | Release and variant builds, then `b3_decode --events` (port 1) against `tools/intent_to_events.py`: Feed A byte-identical, Feed B per channel, identical records in every build | `cpp_baseline/build/*` |
| `make book-compare` | Every book build (uncapped, k16, k32, n2k3, uncapped-n2), then `b3_decode --book` (port 2) against `tools/expected_book.py` with the same N and K byte for byte on both feeds, the book key for CLEAN instruments, and the book counters | `cpp_baseline/build/*` |
| `make book-oracle` | Capped against uncapped with the same N (k16 and k32 against uncapped, n2k3 against uncapped-n2): identical records except K, the depth-incomplete flags and flagged sides' levels; truncation and depth-incomplete statistics | `cpp_baseline/build/*` |
| `make mbo-check` | Uncapped build, then `b3_decode --mbo` on every set and feed: 0 per-order faults while CLEAN, 0 level mismatches against the uncapped book at every close, the book key from the MBO book | `cpp_baseline/build/uncapped` |
| `make goldens` | Port 1 goldens, and port 2 capped goldens for K = 16 and K = 32, for the RTL under `data/golden/<set>/`, with provenance | `cpp_baseline/build/release`, `k16`, `k32` |
| `make counters-compare` | Release build, then `b3_decode --counters` (sequencing, rptSeq, stale flags, every counter) against values derived from manifest.json and intent.jsonl, both feeds of every set | `cpp_baseline/build/release` |
| `make decode-compare` | Release build, then `b3_decode --decode` against intent.jsonl (`compare_decode.py --strict`) on both feeds of every set, the schema set's features and B3's sample packet | `cpp_baseline/build/release` |
| `make decode-crosscheck` | Release build, then `b3_decode` against intent.jsonl and manifest.json on both feeds of every set under `B3_DATA` | `cpp_baseline/build/release` |
| `make pcap-crosscheck` | Release build, then `b3_pcapinfo` against the generator's independent Python pcap reader on every `feedA/feedB.pcap` under `B3_DATA` (default `data/`, filled by `make data`) | `cpp_baseline/build/release` |

All builds use `-Wall -Wextra -Wpedantic -Werror`.

## Generated layouts

`include/b3/layouts.hpp` holds every B3 template's field offsets, sizes, types,
sinceVersion, null values, enums and sets as constexpr values in `b3::layout`
(for example `b3::layout::Order_MBO_50::mDEntryPx::offset`). It is generated
by `tools/gen_layouts.py` from B3's XML, in the same run as the feed
generator's `layouts.py`. Never edit it by hand:

| Target | What it does |
|---|---|
| `make layouts` | Regenerates the three files |
| `make layouts-check` | Regenerates into a temp folder, fails if any checked-in file differs. Run by `make cpp-test` |

`tests/test_layouts.cpp` checks a few values against numbers copied by hand
from `spec/LAYOUTS.md`.

## Pcap reader

`include/b3/pcap.hpp`: `b3::pcap::Capture::from_file(path)` reads the whole
file, then indexes it once into `PacketView`s (frame number, capture time in
ns, UDP payload pointer and length inside the buffer, destination and source
address and port). Classic pcap only (ns or µs, either byte order), Ethernet,
one optional VLAN tag, IPv4 with options, UDP. Frames it skips are counted.
It never reads the UDP payload.

```
cpp_baseline/build/release/b3_pcapinfo [--timing] feedA.pcap   # counters as one JSON line
```

## Packet decoder and message walker

`include/b3/packet.hpp`: parses the 16-byte packet header; datagrams under 16
bytes are discarded and counted. Sequencing is a template parameter
(`AcceptAll` until step 6). `include/b3/walker.hpp`: steps through messages by
messageLength, applies the complete-message test of `docs/DESIGN.md` 3.6.1
(stopping at the first failure, counted by reason), and dispatches each
message by a switch on templateId to a sink given as a template parameter.

`include/b3/decode.hpp`: `decode::get<F>(view)` reads field `F` (a
descriptor from the generated `include/b3/fields.hpp`, for example
`b3::fields::Order_MBO_50::mDEntryPx`) as present, null or absent, by the
rules of `docs/DESIGN.md` 3.6.2; `walk_body` walks groups and var data with
bounds checks. `include/b3/decode_writer.hpp` is the full decode writer (not
for timed code).

```
cpp_baseline/build/release/b3_decode [--list FILE] [--decode FILE.jsonl] feedA.pcap
```

## Sequencing, instruments, stale flags

`packet.hpp`'s `Sequencer` decides each packet from its header and its first
message's header (`docs/DESIGN.md` 3.6.3). `instruments.hpp` loads the
instrument table (`securityID channel` per line, DESIGN.md 4.4; write it with
`tools/gen_instruments.py -o instruments.txt`). `tracker.hpp` follows rptSeq
and stale flags per table instrument (DESIGN.md 3.6.4).

```
cpp_baseline/build/release/b3_decode --instruments instruments.txt --counters counters.json feedA.pcap
```

## Port 1

`records.hpp`: the 64-byte event record and the file header (DESIGN.md 2, 3.1).
`event_sink.hpp`: one record per hot-path message and END_OF_EVENT markers,
per DESIGN.md 3.4 and 3.5, into a buffer allocated before the loop.

```
cpp_baseline/build/release/b3_decode --events events.bin feedA.pcap
python3 tools/dump_events.py events.bin --limit 20
```

## Port 2 (steps 8a and 8b: the books)

`book_capped.hpp` (the default build, the RTL's algorithm, DESIGN.md 4.2): K
slots per side sorted best first, a valid count, a truncated flag and the
boundary price, in one fixed array allocated once; inserts and deletes
compare against all K slots and shift, as the hardware does.
`book_uncapped.hpp` (`B3_BOOK=uncapped`, software only): sorted flat level
arrays per instrument and side, reserved at startup. `book_sink.hpp`:
BOOK_UPDATE, STATE_CHANGE and STATUS records from port 1 records and the
tracker's state changes (DESIGN.md 4), with either book.

```
cpp_baseline/build/k16/b3_decode --instruments instruments.txt --book book.bin feedA.pcap
```

`book_mbo.hpp` and `mbo_check.hpp` (step 9, `--mbo`, uncapped build only, never
timed): the per-order book from port 1 records, its per-order consistency
checks, and at every event close its per-price totals against the uncapped
book (DESIGN.md 4.8).

```
cpp_baseline/build/uncapped/b3_decode --instruments instruments.txt --mbo feedA.pcap
```

## Book parameters

Set in exactly one place, `include/b3/book_params.hpp`. Override them from CMake:

| CMake option | Default | Meaning |
|---|---|---|
| `-DB3_PUBLISHED_DEPTH=N` | 5 | Levels per side in each book update |
| `-DB3_TRACKED_DEPTH=K` | 16 | Levels per side kept internally |
| `-DB3_MAX_INSTRUMENTS=M` | 64 | Books held |
| `-DB3_BOOK=capped\|uncapped` | capped | Capped: K slots per side (the RTL's algorithm). Uncapped: no tracked-depth limit |
| `-DB3_PUBLISH_STALE=ON` | OFF | Port 2 also writes BOOK_UPDATEs for STALE books, marked STALE |

Code reads them as `b3::kPublishedDepth`, `b3::kTrackedDepth`,
`b3::kMaxInstruments` and `b3::kBookMode`. N > K fails to compile.
CMake prints the values it configured (`-- b3: Release, N=5 K=16 ...`), and the
test `book parameters match the build configuration` checks the code saw the same.

Custom build, for example a tiny K for unit tests:

```
cmake -S cpp_baseline -B cpp_baseline/build/k4 -DCMAKE_BUILD_TYPE=Debug -DB3_TRACKED_DEPTH=4 -DB3_PUBLISHED_DEPTH=4
cmake --build cpp_baseline/build/k4
ctest --test-dir cpp_baseline/build/k4 --output-on-failure
```

## Layout

```
CMakeLists.txt
include/b3/book_params.hpp   book parameters (only place they are set)
include/b3/layouts.hpp       generated field layouts (do not edit)
include/b3/fields.hpp        generated field descriptors over layouts.hpp (do not edit)
include/b3/decode.hpp        field accessors, presence rules, groups and var data
include/b3/decode_writer.hpp full decode writer (JSON Lines, debug only)
src/decode_writer.cpp
include/b3/wire.hpp          unaligned little/big-endian loads (memcpy)
include/b3/pcap.hpp          pcap reader
src/pcap.cpp
apps/b3_pcapinfo.cpp         pcap -> counters JSON line
include/b3/packet.hpp        packet header, short datagrams, sequencing hook
include/b3/framing.hpp       the complete message test (walker, sequencer)
include/b3/walker.hpp        message framing, malformed test, dispatch
include/b3/instruments.hpp   instrument table: load and lookup
include/b3/tracker.hpp       rptSeq, stale flags, per-message checks
include/b3/records.hpp       port 1 record and file header structs
include/b3/event_sink.hpp    port 1: records, event closing
include/b3/book_capped.hpp   the capped price-level book (K slots, the RTL's)
include/b3/book_uncapped.hpp the uncapped price-level book
include/b3/book_sink.hpp     port 2: book updates, state changes, status
include/b3/book_mbo.hpp      the MBO (per-order) book, software only
include/b3/mbo_check.hpp     the MBO book against the uncapped book
src/instruments.cpp
apps/b3_decode.cpp           pcap -> decode counts JSON (and --list)
tests/test_build.cpp         doctest main, build parameter tests
tests/test_layouts.cpp       layouts.hpp against spec/LAYOUTS.md
tests/test_wire.cpp          both byte orders, every width
tests/test_pcap.cpp          reader on hand-built pcap bytes
tests/test_walker.cpp        packet decoder and walker on hand-built packets
tests/test_decode.cpp        field access, groups, var data, body faults, writer
tests/test_sequencer.cpp     sequencing rules
tests/test_tracker.cpp       rptSeq, stale flags, unknown instruments
tests/test_instruments.cpp   table loading and lookup
tests/test_events.cpp        port 1 per kind, event closing, DESIGN.md hex examples
tests/test_book.cpp          uncapped book, port 2 rules (both books), DESIGN.md 4.7
tests/test_book_capped.cpp   the capped book's truncation rules at K = 4
tests/test_book_mbo.cpp      the MBO book, its faults, the three-way check
third_party/doctest.h        doctest 2.4.11, vendored (MIT)
```
