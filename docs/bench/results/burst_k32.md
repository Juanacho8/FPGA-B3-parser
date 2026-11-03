**capped K=32, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 18.48 M msg/s, 2.04 M pkt/s (54.1 ns/msg; median over 1024-packet batches 453.6 ns/pkt). Latency passes: 55.2 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 500 (458–500) | 917 (875–917) | 1083 (1042–1125) | 1208 (1167–1292) | 5958 (2625–44667) |
| Port 2 (22128 packets) | 542 (500–542) | 917 (917–958) | 1083 (1083–1125) | 1250 (1167–1292) | 5958 (2667–44958) |
