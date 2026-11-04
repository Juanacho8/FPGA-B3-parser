**capped K=32, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 17.59 M msg/s, 5.69 M pkt/s (56.8 ns/msg; median over 1024-packet batches 107.2 ns/pkt). Latency passes: 58.6 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 417 (417–417) | 958 (958–1042) | 1125 (1083–2333) | 37958 (10833–4149167) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 458 (458–542) | 1000 (958–1125) | 1167 (1125–2542) | 39708 (11000–11018000) |
