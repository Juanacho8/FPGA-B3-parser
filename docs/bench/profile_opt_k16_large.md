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
