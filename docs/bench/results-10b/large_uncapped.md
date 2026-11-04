**uncapped, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 18.44 M msg/s, 5.96 M pkt/s (54.2 ns/msg; median over 1024-packet batches 101.9 ns/pkt). Latency passes: 55.6 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 417 (375–417) | 917 (875–958) | 1083 (1042–2208) | 38667 (11375–1635750) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 417 (417–500) | 958 (917–1042) | 1125 (1083–2375) | 39208 (19500–1636417) |
