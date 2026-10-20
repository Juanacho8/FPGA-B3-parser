"""Layer 5: wire writer.

Wraps packets in Ethernet/IPv4/UDP (big-endian headers, multicast group per
channel) and writes nanosecond-resolution pcap files.

Must not: change packet content.
See docs/FEED_GENERATOR_CONTEXT.md section 10 (Outputs).
"""
