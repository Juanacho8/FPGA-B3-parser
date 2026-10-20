"""Layer 3: market data publisher.

Turns engine outcomes into B3 messages following B3's event rules. Owns
rptSeq, matchEventIndicator (EndOfEvent on the last message of each event),
tradeID assignment and per-instrument statistics. Feeds the intent log.

Must not: know about packet boundaries.
See docs/FEED_GENERATOR_CONTEXT.md sections 5 and 6.
"""
