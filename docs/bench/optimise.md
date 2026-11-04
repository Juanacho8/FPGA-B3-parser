# Optimised builds (step 10b)

The C++'s second role (brief section 11): the timing baseline, any software technique, outputs byte-identical to the goldens. `B3_IMPL=optimised` (CMake) selects it; each change sits beside the mirror's code (`if constexpr` on its switch in `cpp_baseline/include/b3/opt_flags.hpp`, or its own function), so a mirror build compiles exactly the mirror's code. The builds: `make cpp-opt` (optimised capped K = 16 and K = 32, uncapped, and N = 2, K = 3 for truncation on real data), all timed (`B3_TIMED=ON`) and profile-guided. `events-compare`, `book-compare`, `book-oracle` and `cpp-test` (a Debug build of the optimised implementation, sanitizers, every unit test) run them; every change passed the bench's golden gate and `make check-fast` before it was measured.

Machine: Apple M1 Max, macOS 15.7.4 (24G517), Apple LLVM 17.0.0 (clang-1700.0.13.5), on AC power, lid open. Background load varied between runs (Safari playing media; Spotlight and fileproviderd indexing during the final run); each A/B alternates its two binaries, so slow drift cancels.

## Changes, one at a time

Each change was measured against the build before it (A: before, B: after), alternately, 3 rounds of 30 throughput passes on large and on burst (5 rounds where 3 left it inside the noise), Feed A, after the golden gate. Values: the median over rounds of each pass median, ns per message. Noise: the spread of A's rounds, at least 1% of A. Kept when it beats the noise on at least one set and is not slower on the other.

| # | Change | Stage | Build | large: before → after (ns/msg) | burst: before → after (ns/msg) | Verdict | Kept |
|---|---|---|---|---|---|---|---|
| 1 | Book sink: each BOOK_UPDATE written in its place in the output buffer (header whole, levels copied in, only unused levels and padding zeroed); no temporary record, no copy | book sink | K=16 | 55.92 → 53.66 (-2.26) | 52.38 → 48.64 (-3.74) | large faster (noise 1.80), burst faster (noise 0.88) | kept |
| 2 | Capped book: best-first scan of the sorted slots with early exit (most updates are near the top), shifts by memmove of the valid slots only | book | K=16 | 51.82 → 50.95 (-0.87) | 48.97 → 48.08 (-0.89) | large within noise (noise 3.35), burst faster (noise 0.77) | kept |
| 3 | Instrument lookup: an open-addressing hash index built at load in place of the linear scan (about 3 lookups a message: tracker, event sink, book sink) | tracker, sinks | K=16 | 51.23 → 45.05 (-6.18) | 47.48 → 42.82 (-4.66) | large faster (noise 2.38), burst faster (noise 0.75) | kept |
| 4 | Silence check only when capture time crosses the earliest deadline (or goes backwards), instead of a scan of every channel per datagram | tracker | K=16 | 45.48 → 44.80 (-0.68) | 42.40 → 42.26 (-0.14) | large faster (noise 0.49), burst within noise (noise 0.42) | kept |
| 5 | Event sink: each port 1 record built in its place in the output buffer | event sink | K=16 | 44.90 → 46.16 (+1.26) | 42.76 → 43.32 (+0.56) | large slower (noise 0.53), burst slower (noise 0.50) | dropped |
| 6 | Uncapped book: best-first scan from the best end of the flat array in place of the binary search | book | uncapped | 47.30 → 45.60 (-1.70) | 45.23 → 43.67 (-1.56) | large faster (noise 0.51), burst faster (noise 0.90) | kept |
| 7 | Link-time optimisation (-flto) | whole build | K=16 | 45.47 → 46.53 (+1.06) | 43.10 → 44.29 (+1.19) | large within noise (noise 1.52), burst slower (noise 1.15) | dropped |
| 8 | Profile-guided optimisation (clang -fprofile-instr-generate, trained on small; -fprofile-instr-use) | whole build | K=16 | 44.69 → 37.60 (-7.09) | 42.17 → 34.93 (-7.24) | large faster (noise 0.45), burst faster (noise 0.61) | kept |

Notes:
* 2 is about 0.9 ns/msg faster on both sets; on large one slow A round widens the noise band, B is lower in 4 rounds of 5. At K = 32 its effect is larger: the mirror compares all 32 slots per update, the scan stops at the first slot not better (the final K = 32 build runs as fast as K = 16).
* 3: the table holds 4 instruments, but a lookup runs about three times a message and the scan cannot stop early for a securityID not in the table; one probe instead saves 6 ns/msg.
* 4 saves per datagram, so it shows on large (about 3 messages a packet) and not on burst (about 9).
* 5 was slower on both sets (clearing the record in the buffer first costs more than the 64-byte copy it saves): removed.
* 7 (LTO): the hot path is header-only and already inlined within one translation unit; within noise on large, slower on burst: not used.
* 8 (PGO) is the largest single gain. Trained on small, measured on large and burst. `make cpp-opt` trains each build's profile once (`cpp_baseline/build/pgo/`, against the Python answer keys on small); `make cpp-opt-train` retrains after code changes (a stale profile changes speed, never output).
* Not attempted, from the candidate list: a bitmap for the touched set (it is already a small list with a flag array, 2 instruments a channel); NEON compares across the slots (the early-exit scan touches one to three slots on this data, fewer than a 16-wide compare); a STATUS deadline (it is already one compare per datagram); removing checks in the walker (each framing check is part of the contract, and field reads keep their per-field presence check, which is what "absent" means); building port 1 records in place (5, dropped).

## Final: optimised against the mirror

`make bench BENCH_OUT=docs/bench/results-10b`, one run, 30 throughput and 30 latency passes per build, every build after its golden gate. Latencies in ns per packet from its payload reaching the packet decoder (p50 on large is within one or two counter ticks of 41.67 ns, an upper bound).

| Set | Build | ns/msg (min–max) | M msg/s | Port 1 p50 / p90 / p99 / p99.9 | Port 2 p50 / p90 / p99 / p99.9 | max (port 1) |
|---|---|---|---|---|---|---|
| large | mirror K=16 | 56.0 (54.8–61.4) | 17.86 | 42 / 417 / 958 / 1083 | 83 / 458 / 958 / 1125 | 41417 |
| large | **optimised K=16** | 37.6 (36.9–39.0) | 26.57 | 42 / 333 / 750 / 875 | 83 / 375 / 792 / 917 | 32958 |
| large | mirror K=32 | 56.8 (56.0–59.5) | 17.59 | 42 / 417 / 958 / 1125 | 83 / 458 / 1000 / 1167 | 37958 |
| large | **optimised K=32** | 37.4 (36.7–42.4) | 26.76 | 42 / 333 / 750 / 875 | 83 / 375 / 792 / 917 | 34750 |
| large | mirror uncapped | 54.2 (52.7–66.1) | 18.44 | 42 / 417 / 917 / 1083 | 83 / 417 / 958 / 1125 | 38667 |
| large | **optimised uncapped** | 39.2 (38.6–42.8) | 25.48 | 42 / 333 / 792 / 917 | 83 / 375 / 792 / 958 | 37917 |
| burst | mirror K=16 | 53.0 (52.1–54.5) | 18.85 | 500 / 875 / 1042 / 1250 | 542 / 917 / 1083 / 1292 | 21042 |
| burst | **optimised K=16** | 35.8 (34.7–37.4) | 27.91 | 375 / 708 / 875 / 1000 | 417 / 750 / 875 / 1042 | 22917 |
| burst | mirror K=32 | 54.4 (53.2–56.1) | 18.40 | 500 / 917 / 1083 / 1292 | 542 / 917 / 1125 / 1333 | 27375 |
| burst | **optimised K=32** | 36.0 (34.5–37.2) | 27.74 | 375 / 708 / 833 / 1000 | 417 / 750 / 875 / 1042 | 24833 |
| burst | mirror uncapped | 51.4 (49.7–76.0) | 19.45 | 458 / 833 / 1042 / 1208 | 500 / 875 / 1042 / 1250 | 27208 |
| burst | **optimised uncapped** | 37.3 (36.1–38.7) | 26.82 | 417 / 750 / 875 / 1042 | 417 / 750 / 917 / 1042 | 22792 |

Large: the optimised builds are about 33% faster than the mirror (K = 16: 56.0 → 37.6 ns/msg, 26.6 M msg/s), with p90 to p99.9 down 15–20%. The optimised capped book is now faster than the optimised uncapped one: with the scan, its fixed slots beat the uncapped book's larger arrays.

## State size

| Build | Book state (bytes) | Other state added |
|---|---|---|
| capped K = 16 | 53,248, mirror and optimised alike | lookup index 256 bytes, tracker deadlines 16 bytes |
| capped K = 32 | 102,400, alike | the same |
| uncapped | 3,148,800, alike | the same |

The capped optimised build keeps the mirror's memory budget: the same slots, valid count, truncated flag, boundary and null-price bucket per side.

## Profile after the kept changes: optimised K = 16, on large

`make profile-opt`: the optimised K = 16 build with stage entry points out of line (`B3_PROFILE=ON`) and the same PGO profile, throughput passes, `/usr/bin/sample`. Compare `profile_k16_large.md` (the mirror, step 10a).

Profile of `profile-opt-k16` on `data/large/feedA.pcap`: 13404 samples (1 ms, top of stack, self time), 20 s of timed passes.

| Stage | Samples | Share |
|---|---|---|
| book sink | 3738 | 27.9% |
| book | 3123 | 23.3% |
| walker | 2637 | 19.7% |
| event sink | 1274 | 9.5% |
| tracker | 914 | 6.8% |
| field decode | 647 | 4.8% |
| harness | 457 | 3.4% |
| packet decoder and sequencer | 280 | 2.1% |
| field guards | 241 | 1.8% |
| other | 93 | 0.7% |

| Function (top of stack) | Stage | Samples | Share |
|---|---|---|---|
| `b3::book::BookSink<b3::book::CappedBook<16ul, 5ul, 64ul>, false>::on_message` (b3_bench) | book sink | 2777 | 20.7% |
| `b3::walker::Walker::walk<` (b3_bench) | walker | 2637 | 19.7% |
| `b3::book::CappedBook<16ul, 5ul, 64ul>::add` (b3_bench) | book | 1620 | 12.1% |
| `b3::book::CappedBook<16ul, 5ul, 64ul>::remove` (b3_bench) | book | 1347 | 10.0% |
| `b3::book::BookSink<b3::book::CappedBook<16ul, 5ul, 64ul>, false>::apply` (b3_bench) | book sink | 961 | 7.2% |
| `b3::event_sink::EventSink::on<` (b3_bench) | event sink | 486 | 3.6% |
| `main::$_0::operator` (b3_bench) | harness | 457 | 3.4% |
| `b3::event_sink::EventSink::on<` (b3_bench) | event sink | 412 | 3.1% |
| `b3::event_sink::fill<` (b3_bench) | field decode | 338 | 2.5% |
| `b3::tracker::Tracker::on<` (b3_bench) | tracker | 319 | 2.4% |
| `b3::tracker::Tracker::on<` (b3_bench) | tracker | 307 | 2.3% |
| `b3::packet::PacketDecoder<b3::packet::Sequencer>::decode` (b3_bench) | packet decoder and sequencer | 280 | 2.1% |
| `b3::event_sink::fill<` (b3_bench) | field decode | 207 | 1.5% |
| `b3::event_sink::EventSink::on<` (b3_bench) | event sink | 170 | 1.3% |
| `b3::tracker::Tracker::check_appliable<` (b3_bench) | field guards | 161 | 1.2% |
| `b3::book::CappedBook<16ul, 5ul, 64ul>::change` (b3_bench) | book | 156 | 1.2% |
| `b3::tracker::Tracker::on<` (b3_bench) | tracker | 115 | 0.9% |
| `b3::event_sink::EventSink::on<` (b3_bench) | event sink | 104 | 0.8% |
| `b3::event_sink::EventSink::on<` (b3_bench) | event sink | 102 | 0.8% |
| `b3::tracker::Tracker::on<` (b3_bench) | tracker | 87 | 0.6% |
| `b3::tracker::Tracker::on<` (b3_bench) | tracker | 86 | 0.6% |
| `b3::tracker::Tracker::check_appliable<` (b3_bench) | field guards | 80 | 0.6% |
| `b3::event_sink::fill<` (b3_bench) | field decode | 70 | 0.5% |
| `<deduplicated_symbol>` (b3_bench) | other | 41 | 0.3% |
| `madvise` (libsystem_kernel.dylib) | other | 34 | 0.3% |

Where the time moved: the record copies (`_platform_memmove`, 6% of the mirror's samples) are gone, and the tracker fell from 12.7% to 6.8% (the lookup and the silence deadline). PGO inlines the sink's close, sync and record writing into `on_message`, which therefore carries most of the book sink; `CappedBook::match` is now inlined into `add` and `remove` (the book, 23%). The walker (20%) and the event sink with its field decode (14%) are the next largest; the field guards stay under 2%.
