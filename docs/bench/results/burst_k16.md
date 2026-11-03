**capped K=16, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 19.14 M msg/s, 2.12 M pkt/s (52.2 ns/msg; median over 1024-packet batches 443.4 ns/pkt). Latency passes: 54.0 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 458 (458–500) | 875 (875–917) | 1042 (1042–1083) | 1167 (1125–1833) | 9375 (1417–45708) |
| Port 2 (22128 packets) | 500 (500–542) | 875 (875–917) | 1042 (1042–1083) | 1208 (1167–1708) | 9750 (3250–45792) |
