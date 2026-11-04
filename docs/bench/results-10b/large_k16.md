**capped K=16, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 17.86 M msg/s, 5.77 M pkt/s (56.0 ns/msg; median over 1024-packet batches 104.8 ns/pkt). Latency passes: 57.9 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 417 (417–458) | 958 (917–1000) | 1083 (1042–1917) | 41417 (9542–6969625) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 458 (458–500) | 958 (958–1042) | 1125 (1083–2000) | 41750 (13667–6968625) |
