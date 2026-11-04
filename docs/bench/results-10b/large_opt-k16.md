**capped K=16, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 26.57 M msg/s, 8.59 M pkt/s (37.6 ns/msg; median over 1024-packet batches 70.2 ns/pkt). Latency passes: 47.1 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 333 (333–333) | 750 (750–792) | 875 (875–917) | 32958 (15333–52542) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 375 (375–375) | 792 (750–792) | 917 (875–1000) | 33792 (15375–53750) |
