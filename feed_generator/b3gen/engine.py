"""Layer 2: matching engine.

One book per instrument and side, price/priority by secondaryOrderID
(ascending; smaller is first in line). Applies order-flow instructions and
returns outcomes: rested, reduced, requeued, fills.

Must not: know about B3 messages, rptSeq, or packets.
See docs/FEED_GENERATOR_CONTEXT.md section 5 (Engine).
"""
