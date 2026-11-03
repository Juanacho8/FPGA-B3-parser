**capped K=16, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 18.10 M msg/s, 5.85 M pkt/s (55.2 ns/msg; median over 1024-packet batches 103.9 ns/pkt). Latency passes: 57.1 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 417 (417–417) | 958 (917–1000) | 1083 (1042–2292) | 27375 (8083–1051167) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 458 (417–458) | 958 (917–1042) | 1125 (1083–2375) | 36042 (10542–1051208) |

**capped K=32, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 17.69 M msg/s, 5.72 M pkt/s (56.5 ns/msg; median over 1024-packet batches 106.6 ns/pkt). Latency passes: 58.3 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 417 (417–500) | 958 (917–1125) | 1125 (1083–2542) | 37625 (5708–1192833) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 458 (458–542) | 1000 (958–1167) | 1125 (1125–2583) | 37208 (6542–1193000) |

**uncapped, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 18.78 M msg/s, 6.07 M pkt/s (53.3 ns/msg; median over 1024-packet batches 100.5 ns/pkt). Latency passes: 54.7 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 375 (375–417) | 917 (875–917) | 1083 (1042–1542) | 35917 (11583–822667) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 417 (417–458) | 917 (917–958) | 1083 (1042–1583) | 40833 (10875–849000) |

**capped K=16, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 19.14 M msg/s, 2.12 M pkt/s (52.2 ns/msg; median over 1024-packet batches 443.4 ns/pkt). Latency passes: 54.0 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 458 (458–500) | 875 (875–917) | 1042 (1042–1083) | 1167 (1125–1833) | 9375 (1417–45708) |
| Port 2 (22128 packets) | 500 (500–542) | 875 (875–917) | 1042 (1042–1083) | 1208 (1167–1708) | 9750 (3250–45792) |

**capped K=32, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 18.48 M msg/s, 2.04 M pkt/s (54.1 ns/msg; median over 1024-packet batches 453.6 ns/pkt). Latency passes: 55.2 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 500 (458–500) | 917 (875–917) | 1083 (1042–1125) | 1208 (1167–1292) | 5958 (2625–44667) |
| Port 2 (22128 packets) | 542 (500–542) | 917 (917–958) | 1083 (1083–1125) | 1250 (1167–1292) | 5958 (2667–44958) |

**uncapped, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 19.90 M msg/s, 2.20 M pkt/s (50.3 ns/msg; median over 1024-packet batches 425.0 ns/pkt). Latency passes: 51.5 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 458 (458–500) | 833 (833–875) | 1000 (1000–1083) | 1167 (1083–2250) | 14625 (2875–51458) |
| Port 2 (22128 packets) | 500 (500–500) | 875 (833–875) | 1042 (1000–1083) | 1167 (1125–2542) | 15458 (2917–52167) |
