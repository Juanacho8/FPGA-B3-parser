**uncapped, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 25.47 M msg/s, 8.23 M pkt/s (39.3 ns/msg; median over 1024-packet batches 73.7 ns/pkt). Latency passes: 48.5 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 333 (333–333) | 792 (792–833) | 917 (917–1792) | 37917 (10958–52500) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 375 (375–417) | 792 (792–875) | 958 (917–1917) | 39958 (11917–54458) |
