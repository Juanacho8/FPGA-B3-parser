"""Layer 1: order flow.

Produces timestamped order instructions: new limit order, cancel, size
change, price change, aggressive order. Zero-intelligence model with bursty
(regime-switching Poisson) arrival timing, driven by a seeded RNG.

Must not: know about the book's internals or B3 messages.
See docs/FEED_GENERATOR_CONTEXT.md section 8.
"""
