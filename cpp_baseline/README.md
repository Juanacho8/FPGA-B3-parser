# C++ baseline

Software feed handler for B3 Binary UMDF: pcap in, event records (port 1) and
book updates (port 2) out. It is the correctness reference, the RTL's golden
model, and the software timing baseline. Full brief: `docs/CPP_BASELINE_CONTEXT.md`.

Status: step 0 (build system and test harness).

## Requirements

CMake 3.20 or newer and a C++20 compiler (Apple clang 17 tested).
On macOS: `brew install cmake`.

## Build and test

From the repo root:

| Target | What it does | Build folder |
|---|---|---|
| `make cpp-release` | `-O3 -mcpu=native` | `cpp_baseline/build/release` |
| `make cpp-debug` | `-O1 -g`, AddressSanitizer + UndefinedBehaviorSanitizer | `cpp_baseline/build/debug` |
| `make cpp-test` | Debug build, then runs the tests with `ctest` | `cpp_baseline/build/debug` |
| `make cpp-k16` | Release, K = 16 | `cpp_baseline/build/k16` |
| `make cpp-k32` | Release, K = 32 | `cpp_baseline/build/k32` |
| `make cpp-uncapped` | Release, uncapped book | `cpp_baseline/build/uncapped` |
| `make cpp-variants` | The three variants above | |
| `make cpp-clean` | Deletes `cpp_baseline/build` | |

All builds use `-Wall -Wextra -Wpedantic -Werror`.

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
tests/test_build.cpp         doctest tests
third_party/doctest.h        doctest 2.4.11, vendored (MIT)
```
