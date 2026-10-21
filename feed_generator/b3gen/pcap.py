"""Layer 5: wire writer.

Wraps each B3 packet in Ethernet + IPv4 + UDP addressed to its channel's
multicast group and writes a nanosecond-resolution pcap file.

* pcap: magic 0xa1b23c4d (nanosecond timestamps), version 2.4, linktype 1
  (Ethernet). Written little-endian; the record timestamp is the packet's
  time_ns.
* Ethernet: destination MAC 01:00:5e + the low 23 bits of the group address,
  an invented locally administered source MAC, ethertype IPv4.
* IPv4: no options, DF set (never fragmented), TTL 64 by default,
  identification counting up per frame, correct header checksum.
* UDP: correct length and checksum (pseudo-header included).
All network headers are big-endian. The B3 payload inside is little-endian
and is written untouched.

channel_map ({channel: (group, port)}) is a per-call argument: Feed A and
Feed B use different multicast groups. config.py supplies them in step 8.

Must not: change packet content.
See docs/FEED_GENERATOR_CONTEXT.md section 10 (Outputs).
"""

import ipaddress
import struct

PCAP_MAGIC_NS = 0xA1B23C4D
PCAP_VERSION = (2, 4)
SNAPLEN = 65535
LINKTYPE_ETHERNET = 1

ETHERTYPE_IPV4 = 0x0800
IP_PROTO_UDP = 17
IP_FLAG_DF = 0x4000
DEFAULT_TTL = 64
# Locally administered (bit 1 of the first octet set), unicast. Invented.
DEFAULT_SRC_MAC = bytes.fromhex("02b300000001")

ETH_HEADER_SIZE = 14
IP_HEADER_SIZE = 20
UDP_HEADER_SIZE = 8


def multicast_mac(group):
    """01:00:5e followed by the low 23 bits of the IPv4 multicast group."""
    addr = ipaddress.IPv4Address(group)
    if not addr.is_multicast:
        raise ValueError(f"{group} is not a multicast address")
    low23 = int(addr) & 0x7FFFFF
    return bytes([0x01, 0x00, 0x5E]) + low23.to_bytes(3, "big")


def internet_checksum(data):
    """RFC 1071 ones' complement sum of 16-bit big-endian words."""
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def build_frame(payload, *, dst_ip, dst_port, src_ip, src_port, ip_id,
                ttl=DEFAULT_TTL, src_mac=DEFAULT_SRC_MAC):
    """One Ethernet frame carrying payload in UDP to dst_ip:dst_port."""
    src = ipaddress.IPv4Address(src_ip).packed
    dst = ipaddress.IPv4Address(dst_ip).packed
    udp_length = UDP_HEADER_SIZE + len(payload)
    ip_length = IP_HEADER_SIZE + udp_length

    # UDP checksum covers a pseudo-header, the UDP header and the payload.
    pseudo = src + dst + struct.pack("!BBH", 0, IP_PROTO_UDP, udp_length)
    udp_header = struct.pack("!HHHH", src_port, dst_port, udp_length, 0)
    udp_checksum = internet_checksum(pseudo + udp_header + payload)
    if udp_checksum == 0:
        udp_checksum = 0xFFFF  # 0 means "no checksum" in UDP over IPv4
    udp_header = struct.pack("!HHHH", src_port, dst_port, udp_length, udp_checksum)

    ip_header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, ip_length, ip_id & 0xFFFF,
                            IP_FLAG_DF, ttl, IP_PROTO_UDP, 0, src, dst)
    ip_header = ip_header[:10] + struct.pack("!H", internet_checksum(ip_header)) + ip_header[12:]

    eth_header = multicast_mac(dst_ip) + src_mac + struct.pack("!H", ETHERTYPE_IPV4)
    return eth_header + ip_header + udp_header + payload


def pcap_bytes(packets, *, channel_map, src_ip, src_port, ttl=DEFAULT_TTL,
               src_mac=DEFAULT_SRC_MAC):
    """Full pcap file contents for packets [(time_ns, channel, payload)]."""
    out = [struct.pack("<IHHiIII", PCAP_MAGIC_NS, *PCAP_VERSION, 0, 0, SNAPLEN,
                       LINKTYPE_ETHERNET)]
    for ip_id, (time_ns, channel, payload) in enumerate(packets):
        if channel not in channel_map:
            raise ValueError(f"channel {channel} has no multicast group in channel_map")
        group, port = channel_map[channel]
        frame = build_frame(payload, dst_ip=group, dst_port=port, src_ip=src_ip,
                            src_port=src_port, ip_id=ip_id, ttl=ttl, src_mac=src_mac)
        seconds, nanoseconds = divmod(time_ns, 1_000_000_000)
        out.append(struct.pack("<IIII", seconds, nanoseconds, len(frame), len(frame)))
        out.append(frame)
    return b"".join(out)


def write_pcap(path, packets, *, channel_map, src_ip, src_port, ttl=DEFAULT_TTL,
               src_mac=DEFAULT_SRC_MAC):
    """Write packets [(time_ns, channel, payload)] to a pcap file."""
    data = pcap_bytes(packets, channel_map=channel_map, src_ip=src_ip,
                      src_port=src_port, ttl=ttl, src_mac=src_mac)
    with open(path, "wb") as f:
        f.write(data)
