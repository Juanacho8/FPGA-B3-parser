# Benchmark baseline (step 10a)

Run 2026-11-03 on Apple M1 Max (10 cores, 8 performance), macOS 15.7.4 (24G517), Apple LLVM 17.0.0 (clang-1700.0.13.5), `-O3 -DNDEBUG -mcpu=native`, Release, `B3_TIMED=ON`, N = 5, 64 instruments. Now drawing from 'AC Power'; lid open. Background load during the run: Safari playing media (about 10% CPU each for Safari, WebKit and coreaudiod). Clock: the arm64 system counter read directly (`cntvct_el0`), tick 41.67 ns. Each build first passed its golden check (port 1 and port 2 byte-identical). 30 throughput passes (no clock reads in the pipeline) and 30 latency passes per run; values are the median over passes, ranges are min–max over passes. Raw results: `results/*.json`, `results/summary.md`; rerun with `make bench`.

## Throughput

| Set | Build | M msg/s | M pkt/s | ns/msg (min–max) | ns/msg with latency stamps |
|---|---|---|---|---|---|
| large | k16 | 18.10 | 5.85 | 55.2 (54.0–60.8) | 57.1 |
| large | k32 | 17.69 | 5.72 | 56.5 (55.3–60.9) | 58.2 |
| large | uncapped | 18.78 | 6.07 | 53.2 (52.1–56.4) | 54.7 |
| burst | k16 | 19.14 | 2.12 | 52.2 (51.2–54.3) | 54.0 |
| burst | k32 | 18.48 | 2.04 | 54.1 (52.4–56.0) | 55.2 |
| burst | uncapped | 19.90 | 2.20 | 50.3 (48.7–52.2) | 51.5 |

## Latency per packet (ns), from its payload reaching the packet decoder

| Set | Build | Port | p50 | p90 | p99 | p99.9 | max (min–max over passes) |
|---|---|---|---|---|---|---|---|
| large | k16 | 1 | 42 ≈tick | 417 | 958 | 1083 | 27375 (8083–1051167) |
| large | k16 | 2 | 83 ≈tick | 458 | 958 | 1125 | 36042 (10542–1051208) |
| large | k32 | 1 | 42 ≈tick | 417 | 958 | 1125 | 37625 (5708–1192833) |
| large | k32 | 2 | 83 ≈tick | 458 | 1000 | 1125 | 37208 (6542–1193000) |
| large | uncapped | 1 | 42 ≈tick | 375 | 917 | 1083 | 35917 (11583–822667) |
| large | uncapped | 2 | 83 ≈tick | 417 | 917 | 1083 | 40833 (10875–849000) |
| burst | k16 | 1 | 458 | 875 | 1042 | 1167 | 9375 (1417–45708) |
| burst | k16 | 2 | 500 | 875 | 1042 | 1208 | 9750 (3250–45792) |
| burst | k32 | 1 | 500 | 917 | 1083 | 1208 | 5958 (2625–44667) |
| burst | k32 | 2 | 542 | 917 | 1083 | 1250 | 5958 (2667–44958) |
| burst | uncapped | 1 | 458 | 833 | 1000 | 1167 | 14625 (2875–51458) |
| burst | uncapped | 2 | 500 | 875 | 1042 | 1167 | 15458 (2917–52167) |

Reading the numbers:
* **Ticks.** On large most packets hold one message, so the median packet finishes within a tick: p50 of 42 ns (port 1) and 83 ns (port 2) are one and two ticks, an upper bound, not a measurement. The averages (throughput, ns/msg) have no tick error.
* **Tails.** p99.9 is about 1.1 µs everywhere. The max (the largest over a pass) moves by 10–100× between passes, up to about 1 ms: preemption by the OS, not the code. Nothing here pins a core (brief section 8).
* **Builds.** The uncapped book is the fastest (about 3% on large), K = 32 the slowest (about 2% behind K = 16): the capped book compares the price against all K slots on every update, the way the hardware does. No book on this data ever truncates (step 8b), so the three produce the same records.
* **The stamps.** A latency pass reads the counter once per packet and once per record written; it costs 1–2 ns/msg (the last column).

## Profile: capped K = 16, timed, on large

`make profile`: the timed K = 16 build with every stage's entry point kept out of line (`B3_PROFILE=ON`), throughput passes only, sampled by `/usr/bin/sample` at 1 ms for 20 s (Instruments' Time Profiler needs a full Xcode; only the Command Line Tools are installed). Self time (top of stack). The pcap load and index run once before timing and do not appear: about 40 ms for large, outside every timed pass. A profile build adds a call per stage, so shares, not times, are the point.

Profile of `profile-k16` on `data/large/feedA.pcap`: 14750 samples (1 ms, top of stack, self time), 20 s of timed passes.

| Stage | Samples | Share |
|---|---|---|
| book sink | 4355 | 29.5% |
| book | 2722 | 18.5% |
| walker | 2094 | 14.2% |
| tracker | 1869 | 12.7% |
| event sink | 1176 | 8.0% |
| other | 1093 | 7.4% |
| field decode | 633 | 4.3% |
| packet decoder and sequencer | 301 | 2.0% |
| harness | 266 | 1.8% |
| field guards | 241 | 1.6% |

| Function (top of stack) | Stage | Samples | Share |
|---|---|---|---|
| `b3::book::CappedBook<16ul, 5ul, 64ul>::match` (b3_bench) | book | 2338 | 15.9% |
| `b3::walker::Walker::walk<` (b3_bench) | walker | 2094 | 14.2% |
| `b3::book::BookSink<b3::book::CappedBook<16ul, 5ul, 64ul>, false>::close` (b3_bench) | book sink | 1801 | 12.2% |
| `b3::book::BookSink<b3::book::CappedBook<16ul, 5ul, 64ul>, false>::apply` (b3_bench) | book sink | 1135 | 7.7% |
| `b3::book::BookSink<b3::book::CappedBook<16ul, 5ul, 64ul>, false>::sync` (b3_bench) | book sink | 952 | 6.5% |
| `_platform_memmove` (libsystem_platform.dylib) | other | 868 | 5.9% |
| `b3::tracker::Tracker::on<` (b3_bench) | tracker | 669 | 4.5% |
| `b3::tracker::Tracker::on<` (b3_bench) | tracker | 599 | 4.1% |
| `b3::book::BookSink<b3::book::CappedBook<16ul, 5ul, 64ul>, false>::on_message` (b3_bench) | book sink | 467 | 3.2% |
| `b3::event_sink::EventSink::on<` (b3_bench) | event sink | 451 | 3.1% |
| `b3::event_sink::EventSink::on<` (b3_bench) | event sink | 405 | 2.7% |
| `b3::event_sink::fill<` (b3_bench) | field decode | 352 | 2.4% |
| `main::$_0::operator` (b3_bench) | harness | 266 | 1.8% |
| `b3::tracker::Tracker::on<` (b3_bench) | tracker | 215 | 1.5% |
| `b3::book::CappedBook<16ul, 5ul, 64ul>::remove` (b3_bench) | book | 209 | 1.4% |
| `b3::packet::Sequencer::accept` (b3_bench) | packet decoder and sequencer | 194 | 1.3% |
| `b3::tracker::Tracker::on_time` (b3_bench) | tracker | 188 | 1.3% |
| `b3::tracker::Tracker::check_appliable<` (b3_bench) | field guards | 170 | 1.2% |
| `b3::event_sink::fill<` (b3_bench) | field decode | 168 | 1.1% |
| `DYLD-STUB$$memcpy` (b3_bench) | other | 165 | 1.1% |
| `b3::event_sink::EventSink::on<` (b3_bench) | event sink | 143 | 1.0% |
| `b3::book::CappedBook<16ul, 5ul, 64ul>::add` (b3_bench) | book | 135 | 0.9% |
| `b3::tracker::Tracker::on<` (b3_bench) | tracker | 123 | 0.8% |
| `b3::packet::PacketDecoder<b3::packet::Sequencer>::decode` (b3_bench) | packet decoder and sequencer | 107 | 0.7% |
| `b3::event_sink::EventSink::on<` (b3_bench) | event sink | 92 | 0.6% |

Grouped as the brief asks: the book sink (close, apply, sync, on_message) and the book (mostly `CappedBook::match`, the compare against all 16 slots) are about half; the walker (which inlines the framing and dispatch) 14%; the tracker 13%; the event sink and its field decode 12%; the packet decoder and sequencer 2%. `_platform_memmove` (6%) is record copies: a 320-byte port 2 record and a 64-byte port 1 record per write.

## Open item N: the field guards

`Tracker::check_appliable` (the per-message presence checks: price on DELETE, previous size on CHANGE, side) is 1.6% of the profile's samples, an upper bound inflated by the profile build's extra call: about 1 ns per message at most. A direct A/B on large (a local build with the call removed, not committed; three alternating rounds of 30 passes) measured 56.0–56.3 ns/msg with the guards and 55.4–58.8 ns/msg without: **not measurable**, the difference is inside the run-to-run noise (about ±1.5 ns). The field audit's counters are compiled out of timed builds already.

## A harness finding

The first baseline read the clock through `mach_absolute_time` (about 6.5 ns a call) inside the pipeline on every pass: the profile showed it at 28% of samples. The harness now reads `cntvct_el0` directly (0.3 ns) and takes throughput from passes with no clock reads in the pipeline at all; latencies come from separate passes.
