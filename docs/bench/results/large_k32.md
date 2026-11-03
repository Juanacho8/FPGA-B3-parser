**capped K=32, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 17.69 M msg/s, 5.72 M pkt/s (56.5 ns/msg; median over 1024-packet batches 106.6 ns/pkt). Latency passes: 58.3 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 417 (417–500) | 958 (917–1125) | 1125 (1083–2542) | 37625 (5708–1192833) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 458 (458–542) | 1000 (958–1167) | 1125 (1125–2583) | 37208 (6542–1193000) |
