"""events_compare and make_goldens: they pass a correct port 1 file and catch
a wrong record, with its packet and message position."""

import json
import stat
import sys

import events_compare as ec
import make_goldens as mg
from rows import summary, stats, order

TOOLS_DIR = ec.REPO / "tools"


def make_set(tmp_path):
    folder = tmp_path / "data" / "small"
    folder.mkdir(parents=True)
    rows = [order(0, seq=1, mei=0x80), summary(seq=2), stats(seq=2, index=1),
            order(0, seq=1, channel=70, mei=0x80)]
    (folder / "intent.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (folder / "manifest.json").write_text(json.dumps({"missing": {"A": [], "B": []}, "unreliable_books": {}}))
    for feed in "AB":
        (folder / f"feed{feed}.pcap").write_bytes(b"")   # the fake decoder never reads it
    return tmp_path / "data", folder


def fake_cpp(tmp_path, corrupt_record=None):
    """A stand-in for b3_decode --events: the converter's own output for the
    pcap's set, with one record's qty byte changed if asked."""
    script = tmp_path / "fake_decode"
    script.write_text(f"""#!{sys.executable}
import json, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
out, pcap = args[args.index("--events") + 1], Path(args[-1])
subprocess.run([sys.executable, {str(TOOLS_DIR / 'intent_to_events.py')!r}, str(pcap.parent / 'intent.jsonl'),
                "-o", out], check=True, stdout=subprocess.DEVNULL)
data = bytearray(Path(out).read_bytes())
corrupt = {corrupt_record!r}
if corrupt is not None:
    data[32 + 64 * corrupt + 32] ^= 1
Path(out).write_bytes(bytes(data))
print(json.dumps({{"events": {{"records": 0, "markers": 0, "capacity": 0, "overflow": 0,
                                "resets_with_event_open": 0}}}}))
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_correct_file_passes(tmp_path, capsys):
    data, _ = make_set(tmp_path)
    assert ec.main([str(data), "--build", f"release:{fake_cpp(tmp_path)}"]) == 0
    out = capsys.readouterr().out
    assert "OK small/feedA.pcap: 4 records, byte-identical" in out
    assert "OK small/feedB.pcap: 4 records, same records per channel" in out


def test_wrong_record_is_found(tmp_path, capsys):
    data, _ = make_set(tmp_path)
    assert ec.main([str(data), "--build", f"release:{fake_cpp(tmp_path, corrupt_record=1)}"]) == 1
    out = capsys.readouterr().out
    assert "release record 1: first difference at channel 68 v1 packet 2 message 0" in out
    assert "release channel 68 record 1: first difference" in out     # Feed B, per channel
    assert "EXEC_SUMMARY" in out                                      # dump_events format


def test_builds_must_agree(tmp_path, capsys):
    data, _ = make_set(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    assert ec.main([str(data), "--build", f"a:{fake_cpp(tmp_path)}",
                    "--build", f"b:{fake_cpp(other, corrupt_record=0)}"]) == 1
    assert "records of build b differ from a" in capsys.readouterr().out


def test_goldens(tmp_path, capsys):
    data, _ = make_set(tmp_path)
    assert mg.main([str(data), "--cpp", str(fake_cpp(tmp_path))]) == 0
    prov = json.loads((data / "golden" / "small" / "provenance.json").read_text())
    assert set(prov["files"]) == {"expected_feedA.bin", "cpp_feedA.bin", "cpp_feedB.bin"}
    assert prov["files"]["expected_feedA.bin"]["sha256"] == prov["files"]["cpp_feedA.bin"]["sha256"]
    assert prov["build"] == {"published_depth": 5, "tracked_depth": 16, "max_instruments": 64, "book": "capped"}
    assert len(prov["repo"]["commit"]) == 40
    capsys.readouterr()
    bad_data, _ = make_set(tmp_path / "bad")
    assert mg.main([str(bad_data), "--cpp", str(fake_cpp(tmp_path / "bad", corrupt_record=2))]) == 1
    assert "differs from expected_feedA.bin" in capsys.readouterr().out
    assert not (bad_data / "golden" / "small" / "cpp_feedA.bin").exists()
