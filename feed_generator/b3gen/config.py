"""Support: config file loading.

Instruments, channels, securityIDs, ticks, multicast addresses, seed,
message count, packet window, heartbeat interval, burst regime parameters,
Feed B delay and loss rates, all from one file.

Must not: hold logic beyond reading and validating settings.
See docs/FEED_GENERATOR_CONTEXT.md section 10 (Config).
"""
