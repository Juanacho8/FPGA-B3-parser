**capped K=32, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 18.40 M msg/s, 2.04 M pkt/s (54.4 ns/msg; median over 1024-packet batches 462.0 ns/pkt). Latency passes: 55.8 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 500 (500–500) | 917 (875–917) | 1083 (1083–1125) | 1292 (1208–4125) | 27375 (6708–51250) |
| Port 2 (22128 packets) | 542 (500–542) | 917 (917–958) | 1125 (1083–1167) | 1333 (1250–4167) | 27500 (6792–51333) |
