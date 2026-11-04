**uncapped, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 19.45 M msg/s, 2.15 M pkt/s (51.4 ns/msg; median over 1024-packet batches 433.8 ns/pkt). Latency passes: 52.6 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 458 (458–458) | 833 (833–875) | 1042 (1000–1083) | 1208 (1125–3167) | 27208 (1583–1902625) |
| Port 2 (22128 packets) | 500 (500–500) | 875 (833–875) | 1042 (1042–1083) | 1250 (1167–3500) | 27250 (7917–1902167) |
