"""pcap_crosscheck: the Python side on a hand-built pcap, and that a C++
line differing in any way fails the check."""

import json
import stat
import struct
import sys
import zlib

import pcap_crosscheck as pc


def frame(payload, dst=(233, 252, 8, 68), port=30068, proto=17, flags=0x4000):
    udp = struct.pack("!HHHH", 50000, port, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, flags, 64, proto, 0,
                     bytes([10, 0, 0, 1]), bytes(dst))
    return bytes.fromhex("01005e7c0844" "02b300000001" "0800") + ip + udp


def pcap(frames):
    out = [struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, 1)]
    for i, (fr, extra) in enumerate(frames):
        out.append(struct.pack("<IIII", 1, i, len(fr), len(fr) + extra) + fr)
    return b"".join(out)


def write(tmp_path, frames):
    path = tmp_path / "feedA.pcap"
    path.write_bytes(pcap(frames))
    return path


def test_python_side(tmp_path):
    path = write(tmp_path, [(frame(b"\x01" * 10), 0), (frame(b"ab", port=31068), 0),
                            (frame(b"x", proto=6), 0), (frame(b"y", flags=0x2000), 0),
                            (frame(b"z"), 5)])
    info = pc.python_info(path)
    assert (info["frames"], info["udp_payloads"], info["payload_bytes"]) == (5, 2, 12)
    assert (info["non_udp"], info["fragments"], info["truncated"]) == (1, 1, 1)
    assert info["groups"] == {"233.252.8.68:30068": {"packets": 1, "bytes": 10},
                              "233.252.8.68:31068": {"packets": 1, "bytes": 2}}
    crc = zlib.crc32(struct.pack("<QI", 1_000_000_000, 10) + b"\x01" * 10)
    crc = zlib.crc32(struct.pack("<QI", 1_000_000_001, 2) + b"ab", crc)
    assert info["crc32"] == crc


def fake_cpp(tmp_path, line):
    script = tmp_path / "fake_pcapinfo"
    script.write_text(f"#!{sys.executable}\nprint({line!r})\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_match_and_mismatch(tmp_path, capsys):
    path = write(tmp_path, [(frame(b"hello"), 0)])
    good = {"file": str(path), **pc.python_info(path)}
    assert pc.main([str(tmp_path), "--cpp", str(fake_cpp(tmp_path, json.dumps(good)))]) == 0
    for key, value in (("crc32", good["crc32"] ^ 1), ("payload_bytes", 4), ("non_ipv4", 1),
                       ("groups", {})):
        bad = dict(good, **{key: value})
        assert pc.main([str(tmp_path), "--cpp", str(fake_cpp(tmp_path, json.dumps(bad)))]) == 1
        assert f"{key}: python" in capsys.readouterr().out


def test_unsupported_files_fail(tmp_path, capsys):
    path = tmp_path / "feedB.pcap"
    path.write_bytes(pcap([(frame(b"a"), 0)])[:-3])     # record cut off
    assert pc.main([str(tmp_path), "--cpp", "/bin/false"]) == 1
    assert "UNSUPPORTED" in capsys.readouterr().out
