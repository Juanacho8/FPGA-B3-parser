"""pcap writer tests, read back with the independent tests/pcap_reader.py."""

import random

from b3gen import encode, pcap
from b3gen.packet import Packetizer
import pcap_reader

CHANNEL_MAP = {68: ("239.129.2.3", 10068), 70: ("233.1.1.70", 10070)}
SRC_IP = "10.20.30.40"
SRC_PORT = 50000


def sample_packets(seed=3):
    rng = random.Random(seed)
    p = Packetizer([68, 70], start_ns=1_760_000_000_123_456_789, window_ns=5000)
    t = 1_760_000_000_123_456_789
    for i in range(400):
        t += rng.choice([0, 700, 4000, 9000, 1_500_000_000])
        p.add(t, rng.choice([68, 70]),
              encode.order_mbo(securityID=1000 + i, matchEventIndicator=0x80,
                               mDUpdateAction=0, mDEntryType="0", mDEntrySize=100,
                               mDInsertTimestamp=t, secondaryOrderID=i, transactTime=t))
    return p.finish(end_ns=t + 2_000_000_000)


def write_and_read(tmp_path, packets):
    path = tmp_path / "feed.pcap"
    pcap.write_pcap(path, packets, channel_map=CHANNEL_MAP, src_ip=SRC_IP, src_port=SRC_PORT)
    data = path.read_bytes()
    return data, pcap_reader.read_pcap(data)


def test_global_header(tmp_path):
    _, (header, _) = write_and_read(tmp_path, sample_packets())
    assert header == {"magic": 0xA1B23C4D, "version": (2, 4), "thiszone": 0,
                      "sigfigs": 0, "snaplen": 65535, "linktype": 1}


def test_records_round_trip(tmp_path):
    packets = sample_packets()
    _, (_, records) = write_and_read(tmp_path, packets)
    assert len(records) == len(packets)
    for i, ((time_ns, channel, payload), rec) in enumerate(zip(packets, records)):
        group, port = CHANNEL_MAP[channel]
        assert rec["time_ns"] == time_ns and rec["ts_frac"] < 1_000_000_000
        assert rec["incl_len"] == rec["orig_len"] == 14 + 20 + 8 + len(payload)
        assert rec["ethertype"] == 0x0800
        assert rec["src_mac"] == bytes.fromhex("02b300000001")
        assert (rec["ver_ihl"], rec["proto"], rec["ttl"]) == (0x45, 17, 64)
        assert rec["flags_frag"] == 0x4000  # DF, offset 0
        assert rec["ip_id"] == i
        assert rec["ip_total_length"] == 20 + 8 + len(payload)
        assert rec["ip_checksum_ok"]
        assert (rec["src_ip"], rec["dst_ip"]) == (SRC_IP, group)
        assert (rec["src_port"], rec["dst_port"]) == (SRC_PORT, port)
        assert rec["udp_length"] == 8 + len(payload)
        assert rec["udp_checksum_ok"]
        assert rec["payload"] == payload


def test_multicast_mac_uses_low_23_bits(tmp_path):
    _, (_, records) = write_and_read(tmp_path, sample_packets())
    macs = {r["dst_ip"]: r["dst_mac"] for r in records}
    # 239.129.2.3: 129 & 0x7f = 1, so the top bit of the second octet is dropped.
    assert macs["239.129.2.3"] == bytes.fromhex("01005e010203")
    assert macs["233.1.1.70"] == bytes.fromhex("01005e010146")


def test_same_input_gives_identical_pcap(tmp_path):
    first = pcap.pcap_bytes(sample_packets(), channel_map=CHANNEL_MAP,
                            src_ip=SRC_IP, src_port=SRC_PORT)
    second = pcap.pcap_bytes(sample_packets(), channel_map=CHANNEL_MAP,
                             src_ip=SRC_IP, src_port=SRC_PORT)
    assert first == second


def test_unmapped_channel_raises():
    import pytest
    with pytest.raises(ValueError, match="channel_map"):
        pcap.pcap_bytes([(0, 99, b"x")], channel_map=CHANNEL_MAP, src_ip=SRC_IP,
                        src_port=SRC_PORT)
