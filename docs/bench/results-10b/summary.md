**capped K=16, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 17.86 M msg/s, 5.77 M pkt/s (56.0 ns/msg; median over 1024-packet batches 104.8 ns/pkt). Latency passes: 57.9 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 417 (417–458) | 958 (917–1000) | 1083 (1042–1917) | 41417 (9542–6969625) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 458 (458–500) | 958 (958–1042) | 1125 (1083–2000) | 41750 (13667–6968625) |

**capped K=32, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 17.59 M msg/s, 5.69 M pkt/s (56.8 ns/msg; median over 1024-packet batches 107.2 ns/pkt). Latency passes: 58.6 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 417 (417–417) | 958 (958–1042) | 1125 (1083–2333) | 37958 (10833–4149167) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 458 (458–542) | 1000 (958–1125) | 1167 (1125–2542) | 39708 (11000–11018000) |

**uncapped, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 18.44 M msg/s, 5.96 M pkt/s (54.2 ns/msg; median over 1024-packet batches 101.9 ns/pkt). Latency passes: 55.6 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 417 (375–417) | 917 (875–958) | 1083 (1042–2208) | 38667 (11375–1635750) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 417 (417–500) | 958 (917–1042) | 1125 (1083–2375) | 39208 (19500–1636417) |

**capped K=16, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 26.57 M msg/s, 8.59 M pkt/s (37.6 ns/msg; median over 1024-packet batches 70.2 ns/pkt). Latency passes: 47.1 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 333 (333–333) | 750 (750–792) | 875 (875–917) | 32958 (15333–52542) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 375 (375–375) | 792 (750–792) | 917 (875–1000) | 33792 (15375–53750) |

**capped K=32, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 26.76 M msg/s, 8.65 M pkt/s (37.4 ns/msg; median over 1024-packet batches 69.7 ns/pkt). Latency passes: 46.9 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 333 (333–333) | 750 (750–792) | 875 (875–1250) | 34750 (9542–927208) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 375 (375–375) | 792 (750–792) | 917 (875–1292) | 37000 (9542–927375) |

**uncapped, N=5, timed** on `data/large/feedA.pcap`

323255 packets, 1000005 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 25.47 M msg/s, 8.23 M pkt/s (39.3 ns/msg; median over 1024-packet batches 73.7 ns/pkt). Latency passes: 48.5 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (323254 packets) | 42 (42–42) ≈tick | 333 (333–333) | 792 (792–833) | 917 (917–1792) | 37917 (10958–52500) |
| Port 2 (323248 packets) | 83 (83–83) ≈tick | 375 (375–417) | 792 (792–875) | 958 (917–1917) | 39958 (11917–54458) |

**capped K=16, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 18.85 M msg/s, 2.09 M pkt/s (53.0 ns/msg; median over 1024-packet batches 449.5 ns/pkt). Latency passes: 54.9 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 500 (458–500) | 875 (875–917) | 1042 (1042–1125) | 1250 (1167–6583) | 21042 (4542–78292) |
| Port 2 (22128 packets) | 542 (500–542) | 917 (875–958) | 1083 (1042–1167) | 1292 (1167–6875) | 21458 (3833–78333) |

**capped K=32, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 18.40 M msg/s, 2.04 M pkt/s (54.4 ns/msg; median over 1024-packet batches 462.0 ns/pkt). Latency passes: 55.8 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 500 (500–500) | 917 (875–917) | 1083 (1083–1125) | 1292 (1208–4125) | 27375 (6708–51250) |
| Port 2 (22128 packets) | 542 (500–542) | 917 (917–958) | 1125 (1083–1167) | 1333 (1250–4167) | 27500 (6792–51333) |

**uncapped, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 19.45 M msg/s, 2.15 M pkt/s (51.4 ns/msg; median over 1024-packet batches 433.8 ns/pkt). Latency passes: 52.6 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 458 (458–458) | 833 (833–875) | 1042 (1000–1083) | 1208 (1125–3167) | 27208 (1583–1902625) |
| Port 2 (22128 packets) | 500 (500–500) | 875 (833–875) | 1042 (1042–1083) | 1250 (1167–3500) | 27250 (7917–1902167) |

**capped K=16, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 27.91 M msg/s, 3.09 M pkt/s (35.8 ns/msg; median over 1024-packet batches 305.3 ns/pkt). Latency passes: 44.7 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 375 (375–417) | 708 (708–750) | 875 (833–917) | 1000 (917–2000) | 22917 (3333–81917) |
| Port 2 (22128 packets) | 417 (417–417) | 750 (708–750) | 875 (833–917) | 1042 (958–2083) | 25250 (5083–81958) |

**capped K=32, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 27.74 M msg/s, 3.07 M pkt/s (36.0 ns/msg; median over 1024-packet batches 308.3 ns/pkt). Latency passes: 44.8 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 375 (375–417) | 708 (708–750) | 833 (833–875) | 1000 (917–3083) | 24833 (8917–220333) |
| Port 2 (22128 packets) | 417 (417–417) | 750 (708–750) | 875 (833–917) | 1042 (917–3250) | 27083 (8917–220375) |

**uncapped, N=5, timed** on `data/burst/feedA.pcap`

22131 packets, 200007 messages; 30 throughput and 30 latency passes; clock tick 41.67 ns. Throughput 26.82 M msg/s, 2.97 M pkt/s (37.3 ns/msg; median over 1024-packet batches 315.8 ns/pkt). Latency passes: 46.0 ns/msg with the stamps.

| Latency (ns, median over passes, min–max) | p50 | p90 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| Port 1 (22131 packets) | 417 (375–417) | 750 (708–750) | 875 (875–917) | 1042 (958–3292) | 22792 (4458–48583) |
| Port 2 (22128 packets) | 417 (417–458) | 750 (750–792) | 917 (875–917) | 1042 (1000–2917) | 22167 (8833–48708) |
