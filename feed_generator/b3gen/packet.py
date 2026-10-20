"""Layer 4: packetizer.

Groups encoded messages into packets (option 3: close on 1400 bytes or on a
time window), stamps packet headers (channel, sequenceVersion,
sequenceNumber, sendingTime), emits heartbeats, and produces Feed A and
Feed B.

Must not: change message content.
See docs/FEED_GENERATOR_CONTEXT.md section 7.
"""
