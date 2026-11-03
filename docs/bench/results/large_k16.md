**capped K=16, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 18.10 M msg/s, 5.85 M pkt/s (55.2 ns/msg; median over 1024-packet batches 103.9 ns/pkt). Latency passes: 57.1 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 417 (417–417) | 958 (917–1000) | 1083 (1042–2292) | 27375 (8083–1051167) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 458 (417–458) | 958 (917–1042) | 1125 (1083–2375) | 36042 (10542–1051208) |
