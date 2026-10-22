"""b3gen: a B3 Binary UMDF incremental feed generator.

Pretends to be B3's market data system: produces the UDP packets B3 would
multicast on the incremental stream (as pcap files) plus answer keys
describing what those packets mean.

Five layers, each feeding the next:

    flow -> engine -> publisher -> packet -> pcap
    (intents) (book + fills) (B3 messages + keys) (packets) (pcap files)

Support modules: layouts, encode (wire format), golden (answer keys),
scenarios, config (TOML file), main (wiring), cli (python -m b3gen).

See docs/FEED_GENERATOR_CONTEXT.md section 4.
"""
