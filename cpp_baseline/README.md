# C++ baseline

Software feed handler for B3 Binary UMDF: pcap in, event records (port 1) and
book updates (port 2) out. It is the correctness reference, the RTL's golden
model, and the software timing baseline. Full brief: `docs/CPP_BASELINE_CONTEXT.md`.

Status: step 3 (build system, generated field layouts, pcap reader).

## Requirements

CMake 3.20 or newer and a C++20 compiler (Apple clang 17 tested).
On macOS: `brew install cmake`.

## Build and test

From the repo root:

| Target | What it does | Build folder |
|---|---|---|
| `make cpp-release` | `-O3 -mcpu=native` | `cpp_baseline/build/release` |
| `make cpp-debug` | `-O1 -g`, AddressSanitizer + UndefinedBehaviorSanitizer | `cpp_baseline/build/debug` |
| `make cpp-test` | `make layouts-check`, Debug build, then runs the tests with `ctest` | `cpp_baseline/build/debug` |
| `make cpp-k16` | Release, K = 16 | `cpp_baseline/build/k16` |
| `make cpp-k32` | Release, K = 32 | `cpp_baseline/build/k32` |
| `make cpp-uncapped` | Release, uncapped book | `cpp_baseline/build/uncapped` |
| `make cpp-variants` | The three variants above | |
| `make cpp-clean` | Deletes `cpp_baseline/build` | |
| `make pcap-crosscheck` | Release build, then `b3_pcapinfo` against the generator's independent Python pcap reader on every `feedA/feedB.pcap` under `B3_DATA` (default `feed_generator/data`) | `cpp_baseline/build/release` |

All builds use `-Wall -Wextra -Wpedantic -Werror`.

## Generated layouts

`include/b3/layouts.hpp` holds every B3 template's field offsets, sizes, types,
sinceVersion, null values, enums and sets as constexpr values in `b3::layout`
(for example `b3::layout::Order_MBO_50::mDEntryPx::offset`). It is generated
by `tools/gen_layouts.py` from B3's XML, in the same run as the feed
generator's `layouts.py`. Never edit it by hand:

| Target | What it does |
|---|---|
| `make layouts` | Regenerates both files |
| `make layouts-check` | Regenerates into a temp folder, fails if either checked-in file differs. Run by `make cpp-test` |

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

## Book parameters

Set in exactly one place, `include/b3/book_params.hpp`. Override them from CMake:

| CMake option | Default | Meaning |
|---|---|---|
| `-DB3_PUBLISHED_DEPTH=N` | 5 | Levels per side in each book update |
| `-DB3_TRACKED_DEPTH=K` | 16 | Levels per side kept internally |
| `-DB3_MAX_INSTRUMENTS=M` | 64 | Books held |
| `-DB3_BOOK=capped\|uncapped` | capped | Capped: K slots per side (the RTL's algorithm). Uncapped: no tracked-depth limit |

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
include/b3/wire.hpp          unaligned little/big-endian loads (memcpy)
include/b3/pcap.hpp          pcap reader
src/pcap.cpp
apps/b3_pcapinfo.cpp         pcap -> counters JSON line
tests/test_build.cpp         doctest main, build parameter tests
tests/test_layouts.cpp       layouts.hpp against spec/LAYOUTS.md
tests/test_wire.cpp          both byte orders, every width
tests/test_pcap.cpp          reader on hand-built pcap bytes
third_party/doctest.h        doctest 2.4.11, vendored (MIT)
```
