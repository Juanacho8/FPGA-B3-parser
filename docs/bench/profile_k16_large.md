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
