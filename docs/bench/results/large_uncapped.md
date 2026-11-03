**uncapped, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 18.78 M msg/s, 6.07 M pkt/s (53.3 ns/msg; median over 1024-packet batches 100.5 ns/pkt). Latency passes: 54.7 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 375 (375–417) | 917 (875–917) | 1083 (1042–1542) | 35917 (11583–822667) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 417 (417–458) | 917 (917–958) | 1083 (1042–1583) | 40833 (10875–849000) |
