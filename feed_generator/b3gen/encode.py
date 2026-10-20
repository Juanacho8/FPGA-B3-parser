"""Support: one encoder per template.

Turns field values into message bytes (12-byte message header + body) using
the offsets in layouts. Little-endian throughout.

Must not: decide field values (that is the publisher's job) or group
messages into packets.
See spec/LAYOUTS.md.
"""
