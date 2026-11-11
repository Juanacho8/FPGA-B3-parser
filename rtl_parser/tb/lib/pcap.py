# Read UDP payloads (= B3 packets) from a feed_generator pcap.
#
# 1. pcap: 24-byte file header, then per frame a 16-byte record header
#    (ts_sec, ts_frac, incl_len, orig_len, little-endian) and the frame.
# 2. Frame: Ethernet (14) + IPv4 (IHL x 4) + UDP (8), then the payload.

import struct


def payloads(path):
    data = open(path, "rb").read()
    off = 24
    while off < len(data):
        # ---- 1. Record header ----
        _, _, incl_len, _ = struct.unpack_from("<IIII", data, off)
        frame = data[off + 16 : off + 16 + incl_len]
        off += 16 + incl_len

        # ---- 2. Strip Ethernet, IPv4, UDP ----
        ihl = (frame[14] & 0x0F) * 4
        yield frame[14 + ihl + 8 :]
