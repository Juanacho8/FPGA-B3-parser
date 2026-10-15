# C++ baseline

Software implementation of the same feed handler (B3 UMDF pcap in, normalized
records out). Two jobs:

1. Correctness reference: its output must match the FPGA output for the same pcap.
2. Latency baseline: the number the FPGA is measured against.

TODO (Phase 6): build system, pcap reader, SBE decoder, normalized record output.
