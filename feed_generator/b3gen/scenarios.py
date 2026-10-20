"""Support: scenario injection.

Loss, gaps, idle heartbeats, weekly start, engine failover, ChannelReset,
single-instrument reset, new trading session, edge orders, schema
evolution, rare messages and malformed packets. Each scenario goes in its
own output file, separate from the clean main file.

Must not: bypass the layers; scenarios drive the normal pipeline or apply
documented packet-level faults.
See docs/FEED_GENERATOR_CONTEXT.md section 9.
"""
