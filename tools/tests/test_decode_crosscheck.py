"""decode_crosscheck: expected counts from intent rows and manifest faults,
messageLength from rows, and that a differing C++ output fails."""

import json
import stat
import sys

import pytest

import b3intent
import decode_crosscheck as dc
from conftest import generated_sets
from rows import heartbeat, order, row, stats
from test_pcap_crosscheck import frame, pcap


def news(text, headline="", url="", block=36):
    return row(5, {"securityID": 0, "matchEventIndicator": 128, "headline": headline,
                   "text": text, "uRLLink": url}, blockLength=block)


def test_message_length():
    assert dc.message_length(order(0, blockLength=72)) == 84
    assert dc.message_length(order(0, blockLength=80, version=18)) == 92
    assert dc.message_length(heartbeat(blockLength=4)) == 16
    assert dc.message_length(news("abc", headline="h", block=44)) == 12 + 44 + 2 + 1 + 2 + 3 + 2
    assert dc.message_length(news("é")) == 12 + 36 + 2 + 2 + 2 + 2   # UTF-8 bytes, not chars
    assert dc.message_length(row(999, {"body": "00" * 20}, blockLength=20)) == 32
    with pytest.raises(dc.Unsupported):
        dc.message_length(row(71, {}, blockLength=8))


def write_set(tmp_path, rows, manifest):
    (tmp_path / "intent.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path / "intent.jsonl"


def test_expected_counts_and_faults(tmp_path):
    rows = [order(0, seq=1, index=0, blockLength=72), stats(seq=1, index=1, blockLength=52),
            order(0, seq=2, index=0, blockLength=72), order(0, seq=2, index=1, blockLength=72),
            heartbeat(blockLength=4), row(999, {"body": "00"}, seq=3, blockLength=1)]
    manifest = {"missing": {"A": [[68, 1, 3]], "B": []},
                "faults": [{"fault": "short", "channel": 68, "after": [68, 1, 1], "length": 10},
                           {"fault": "bad_encoding", "channel": 68, "sequenceVersion": 1,
                            "sequenceNumber": 2, "offset": 100, "messages_before": 1},
                           {"fault": "truncated", "channel": 68, "sequenceVersion": 1,
                            "sequenceNumber": 3, "offset": 30, "messages_before": 1}]}
    intent = write_set(tmp_path, rows, manifest)
    counts, listing = dc.expected(intent, manifest, "A")
    assert counts["packets"] == 3 and counts["short_datagrams"] == 1 and counts["datagrams"] == 4
    assert counts["messages"] == 4                       # seq 2's second message is cut
    assert counts["malformed"]["bad_encoding"] == 1
    assert counts["malformed"]["header_truncated"] == 0  # that packet is missing from A
    assert counts["templates"] == {"2": 1, "50": 2, "56": 1}
    assert listing[1] == (68, 1, 1, 1, 56, 64)
    counts_b, _ = dc.expected(intent, manifest, "B")
    assert counts_b["malformed"]["header_truncated"] == 1
    assert counts_b["unknown_templates"] == 1


def fake_cpp(tmp_path, counts, listing):
    script = tmp_path / "fake_decode"
    lines = "".join(" ".join(map(str, e)) + "\n" for e in listing)
    script.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"open(sys.argv[2], 'w').write({lines!r})\n"
        f"print({json.dumps({'file': 'x', **counts})!r})\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_match_and_mismatch(tmp_path, capsys):
    data = tmp_path / "data"
    folder = data / "small"
    folder.mkdir(parents=True)
    rows = [order(0, seq=1, index=0, blockLength=72), order(0, seq=1, index=1, blockLength=72),
            order(0, seq=1, index=0, channel=70, blockLength=72)]
    manifest = {"missing": {"A": [], "B": []}}
    intent = write_set(folder, rows, manifest)
    two_packets = pcap([(frame(b"\x00" * 100), 0), (frame(b"\x00" * 100, port=30070), 0)])
    for feed in "AB":
        (folder / f"feed{feed}.pcap").write_bytes(two_packets)   # guard 2 counts 2 packets
    counts, listing = dc.expected(intent, manifest, "A")
    assert dc.main([str(data), "--cpp", str(fake_cpp(tmp_path, counts, listing))]) == 0

    # Feed B may interleave channels differently; Feed A may not.
    swapped = [listing[2], listing[0], listing[1]]
    assert dc.main([str(data), "--cpp", str(fake_cpp(tmp_path, counts, swapped))]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH small/feedA.pcap" in out and "OK small/feedB.pcap" in out

    for bad_counts, bad_list in (
            (dict(counts, messages=2), listing),
            (dict(counts, malformed=dict(counts["malformed"], length_zero=1)), listing),
            (counts, listing[:2]),
            (counts, [listing[0], (*listing[1][:5], 85), listing[2]])):
        assert dc.main([str(data), "--cpp", str(fake_cpp(tmp_path, bad_counts, bad_list))]) == 1


def test_missing_pcap_is_reported(tmp_path, capsys):
    write_set(tmp_path, [order(0)], {"missing": {"A": [], "B": []}})
    assert dc.main([str(tmp_path), "--cpp", "/bin/false"]) == 1   # nothing checked: fail
    captured = capsys.readouterr()
    assert captured.out.count("SKIPPED") == 2
    assert "no pcap was checked" in captured.err


# ---- Guards 1 to 3 on altered copies of the generator's malformed set ----
def copy_set(needs_data, tmp_path, name="scenarios/malformed"):
    src = dict(generated_sets())[name]
    dst = tmp_path / "data" / name
    dst.mkdir(parents=True)
    for f in ("intent.jsonl", "manifest.json", "feedA.pcap", "feedB.pcap"):
        (dst / f).write_bytes((src / f).read_bytes())
    return tmp_path / "data", dst


def edit_manifest(folder, change):
    m = json.loads((folder / "manifest.json").read_text())
    change(m)
    (folder / "manifest.json").write_text(json.dumps(m))


def first_packet_fault(m):
    return next(f for f in m["faults"] if f["fault"] != "short")


def run_guarded(data, capsys):
    # --cpp /bin/false: a guard that does not fire lets the run reach the C++
    # and crash, so each test also proves its guard runs before the C++.
    assert dc.main([str(data), "--cpp", "/bin/false"]) == 1
    return capsys.readouterr().out


def test_guard1_fault_at_message_0(needs_data, tmp_path, capsys):
    data, folder = copy_set(needs_data, tmp_path)
    edit_manifest(folder, lambda m: first_packet_fault(m).update(messages_before=0))
    out = run_guarded(data, capsys)
    assert "messages_before 0: the intent log can no longer give the packet count" in out
    assert "scenarios/malformed/manifest.json: faults[1]" in out


def test_guard1_messages_before_missing(needs_data, tmp_path, capsys):
    data, folder = copy_set(needs_data, tmp_path)
    edit_manifest(folder, lambda m: first_packet_fault(m).pop("messages_before"))
    out = run_guarded(data, capsys)
    assert "messages_before missing: the intent log can no longer give the packet count" in out


def test_guard2_index0_row_removed(needs_data, tmp_path, capsys):
    data, folder = copy_set(needs_data, tmp_path)
    lines = (folder / "intent.jsonl").read_text().splitlines(keepends=True)
    at = next(i for i, ln in enumerate(lines)
              if json.loads(ln)["index"] == 0 and json.loads(ln)["sequenceNumber"] == 100)
    del lines[at]
    (folder / "intent.jsonl").write_text("".join(lines))
    out = run_guarded(data, capsys)
    assert "the 'one row at index 0 per packet' assumption broke" in out
    assert "scenarios/malformed/feedA.pcap: the intent log gives 757 packets" in out
    assert "the pcap has 758 datagrams of at least 16 bytes" in out


def test_guard3_unknown_fault_kind(needs_data, tmp_path, capsys):
    data, folder = copy_set(needs_data, tmp_path)
    edit_manifest(folder, lambda m: first_packet_fault(m).update(fault="checksum_wrong"))
    out = run_guarded(data, capsys)
    assert "unknown fault kind 'checksum_wrong'" in out


@pytest.mark.parametrize("change, message", [
    (lambda m: m.update(missing=[]), "'missing' must be an object with keys 'A' and 'B'"),
    (lambda m: m["missing"].update(A=[[68, 1]]), "'missing.A' must be a list of"),
    (lambda m: m.update(faults={}), "'faults' must be a list"),
    (lambda m: first_packet_fault(m).update(sequenceNumber="140"), "needs an integer 'sequenceNumber'"),
])
def test_guard3_manifest_shape(needs_data, tmp_path, capsys, change, message):
    data, folder = copy_set(needs_data, tmp_path)
    edit_manifest(folder, change)
    assert message in run_guarded(data, capsys)


def test_guard3_every_tool_refuses_a_bad_manifest(needs_data, tmp_path):
    _, folder = copy_set(needs_data, tmp_path)
    edit_manifest(folder, lambda m: first_packet_fault(m).update(fault="checksum_wrong"))
    with pytest.raises(b3intent.ManifestError, match="unknown fault kind"):
        b3intent.load_manifest(folder / "intent.jsonl")
    import intent_to_events
    with pytest.raises(b3intent.ManifestError):
        intent_to_events.main([str(folder / "intent.jsonl"), "-o", str(tmp_path / "e.bin")])


def test_guards_pass_on_the_real_set(needs_data, tmp_path):
    """The unaltered copy passes guards 1 to 3 (no C++ needed for that)."""
    data, folder = copy_set(needs_data, tmp_path)
    raw = json.loads((folder / "manifest.json").read_text())
    dc.check_fault_positions(raw, "malformed")
    manifest = b3intent.validate_manifest(raw)
    for feed in "AB":
        counts, _ = dc.expected(folder / "intent.jsonl", manifest, feed)
        dc.check_packet_count(counts["packets"], folder / f"feed{feed}.pcap", feed)
