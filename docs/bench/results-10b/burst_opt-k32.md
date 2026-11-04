**capped K=32, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 27.74 M msg/s, 3.07 M pkt/s (36.0 ns/msg; median over 1024-packet batches 308.3 ns/pkt). Latency passes: 44.8 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 375 (375–417) | 708 (708–750) | 833 (833–875) | 1000 (917–3083) | 24833 (8917–220333) |
| Port 2 (22128 packets) | 417 (417–417) | 750 (708–750) | 875 (833–917) | 1042 (917–3250) | 27083 (8917–220375) |
