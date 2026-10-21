"""Independent pcap reader for tests. struct only; never imports b3gen.

Parses a nanosecond pcap of Ethernet/IPv4/UDP frames and verifies the
checksums with its own code, so a bug in b3gen.pcap cannot hide itself.
"""

import struct


def checksum16(data):
    """Ones' complement sum, computed independently of b3gen.pcap."""
    if len(data) % 2:
        data = data + b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) | data[i + 1]
    while total > 0xFFFF:
        total = (total & 0xFFFF) + (total >> 16)
    return 0xFFFF - total


def read_pcap(data):
    """Return (global header dict, [record dict]) for a pcap file's bytes."""
    magic, v_major, v_minor, thiszone, sigfigs, snaplen, linktype = \
        struct.unpack_from("<IHHiIII", data, 0)
    header = {"magic": magic, "version": (v_major, v_minor), "thiszone": thiszone,
              "sigfigs": sigfigs, "snaplen": snaplen, "linktype": linktype}
    records = []
    pos = 24
    while pos < len(data):
        ts_sec, ts_frac, incl_len, orig_len = struct.unpack_from("<IIII", data, pos)
        frame = data[pos + 16:pos + 16 + incl_len]
        assert len(frame) == incl_len, "truncated record"
        records.append(parse_frame(frame, ts_sec, ts_frac, orig_len))
        pos += 16 + incl_len
    return header, records


def parse_frame(frame, ts_sec, ts_frac, orig_len):
    dst_mac, src_mac, ethertype = frame[0:6], frame[6:12], struct.unpack_from("!H", frame, 12)[0]
    ip = frame[14:34]
    (ver_ihl, tos, total_length, ident, flags_frag, ttl, proto, ip_checksum,
     src_ip, dst_ip) = struct.unpack("!BBHHHBBH4s4s", ip)
    udp = frame[34:34 + total_length - 20]
    src_port, dst_port, udp_length, udp_checksum = struct.unpack_from("!HHHH", udp, 0)
    pseudo = src_ip + dst_ip + bytes([0, proto]) + struct.pack("!H", udp_length)
    return {
        "time_ns": ts_sec * 1_000_000_000 + ts_frac,
        "ts_frac": ts_frac,
        "incl_len": len(frame), "orig_len": orig_len,
        "dst_mac": dst_mac, "src_mac": src_mac, "ethertype": ethertype,
        "ver_ihl": ver_ihl, "tos": tos, "ip_total_length": total_length,
        "ip_id": ident, "flags_frag": flags_frag, "ttl": ttl, "proto": proto,
        # A correct header sums to zero, including its own checksum field.
        "ip_checksum_ok": checksum16(ip) == 0,
        "src_ip": ".".join(map(str, src_ip)), "dst_ip": ".".join(map(str, dst_ip)),
        "src_port": src_port, "dst_port": dst_port, "udp_length": udp_length,
        "udp_checksum": udp_checksum,
        "udp_checksum_ok": udp_checksum != 0 and checksum16(pseudo + udp) == 0,
        "payload": bytes(udp[8:udp_length]),
    }
