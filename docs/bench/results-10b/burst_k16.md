**capped K=16, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 18.85 M msg/s, 2.09 M pkt/s (53.0 ns/msg; median over 1024-packet batches 449.5 ns/pkt). Latency passes: 54.9 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 500 (458–500) | 875 (875–917) | 1042 (1042–1125) | 1250 (1167–6583) | 21042 (4542–78292) |
| Port 2 (22128 packets) | 542 (500–542) | 917 (875–958) | 1083 (1042–1167) | 1292 (1167–6875) | 21458 (3833–78333) |
