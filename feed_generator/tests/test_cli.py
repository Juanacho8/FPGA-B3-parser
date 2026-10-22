"""The CLI: every set from one config, same bytes as the library, clear
errors, complete provenance, streaming identical to the in-memory writer."""

import filecmp
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from b3gen import cli, config, layouts, main
from layouts_md import MESSAGE_LAYOUTS
import sbe_decoder
from wire import decode_wire

TINY = Path(__file__).parent / "fixtures" / "tiny_config.toml"
FILES = {"feedA.pcap", "feedB.pcap", "intent.jsonl", "book_key.jsonl", "manifest.json",
         "provenance.json"}
SET_FOLDERS = ("small", "large", "burst")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("cli")
    argv = ["build", "--config", str(TINY), "--out", str(out)]
    assert cli.main_cli(argv) == 0
    return out, argv


def folders(out):
    return [out / s for s in SET_FOLDERS] + [out / "scenarios" / n for n in main.SCENARIOS]


def manifest(folder):
    return json.loads((folder / "manifest.json").read_text())


def test_every_set_and_scenario_folder_is_complete(built):
    out, _ = built
    assert sorted(p.name for p in (out / "scenarios").iterdir()) == sorted(main.SCENARIOS)
    for folder in folders(out):
        assert {p.name for p in folder.iterdir()} == FILES, folder


def test_targets_come_from_the_config(built):
    out, _ = built
    sizes = config.load(TINY).sets
    for name, target in (("small", sizes.small_messages), ("large", sizes.large_messages),
                         ("burst", sizes.burst_messages)):
        m = manifest(out / name)
        assert m["target_messages"] == target and m["messages"] >= target
    for name in main.SCENARIOS:
        m = manifest(out / "scenarios" / name)
        assert m["target_messages"] == (main.MALFORMED_MESSAGES if name == "malformed"
                                        else sizes.scenario_messages)


def test_burst_set_spikes_more_often(built):
    out, _ = built
    prov = json.loads((out / "burst" / "provenance.json").read_text())
    assert prov["set"] == "burst"
    settings = config.load(TINY)
    burst = settings.run_config(out, 10, spike_every_ns=settings.sets.burst_spike_every_ns)
    assert burst.flow.spike_every_ns == 50_000_000 < settings.base.flow.spike_every_ns


def independent_rows(folder, securities):
    for (channel, version, seq, index, sending), data in decode_wire(folder / "feedA.pcap"):
        header, fields = sbe_decoder.decode(data)
        if fields is None:
            yield {"templateId": header["templateId"], "sequenceNumber": seq, "index": index}
            continue
        optional = MESSAGE_LAYOUTS[header["templateId"]]["optional_nulls"]
        yield {"channel": channel, "sequenceVersion": version, "sequenceNumber": seq,
               "index": index, "sendingTime": sending, "templateId": header["templateId"],
               "blockLength": header["blockLength"], "version": header["version"],
               "instrument": securities.get(fields.get("securityID")),
               "fields": {k: (v.decode() if isinstance(v, bytes) else v) for k, v in fields.items()},
               "null": [k for k in fields if k in optional and
                        (fields[k] == optional[k] or (isinstance(fields[k], str) and fields[k] == ""))]}


def test_every_pcap_decodes_to_its_intent_log(built):
    out, _ = built
    securities = {i.security_id: i.name for i in config.load(TINY).base.instruments}
    for folder in folders(out):
        if folder.name == "malformed":
            continue  # faults on purpose: tests/test_injections.py
        missing = {tuple(k) for k in manifest(folder)["missing"]["A"]}
        intent = [r for r in (json.loads(l) for l in
                              (folder / "intent.jsonl").read_text().splitlines())
                  if (r["channel"], r["sequenceVersion"], r["sequenceNumber"]) not in missing]
        wire = list(independent_rows(folder, securities))
        assert len(wire) == len(intent), folder
        for got, row in zip(wire, intent):
            if row["templateId"] == main.UNKNOWN_TEMPLATE:
                continue
            row = {k: v for k, v in row.items() if k != "news"}
            assert got == row, (folder, row)


def test_every_manifest_lists_what_its_scenario_promises(built):
    # No scenario may pass empty: each one placed what it is for.
    out, _ = built
    for name in main.SCENARIOS:
        m = manifest(out / "scenarios" / name)
        if name == "loss":
            assert m["missing"]["A"] and not m["missing"]["B"]
        elif name == "gap":
            assert m["missing"]["B"] and m["unreliable_books"]
        elif name == "gap_one":
            assert len(m["missing"]["B"]) == 1 and len(m["unreliable_books"]) == 1
        elif name == "idle":
            assert m["heartbeats"] > 0 and m["params"]["idle"]
        elif name in ("failover", "channel_reset", "instrument_reset", "weekly_start"):
            (reset,) = m["resets"]
            assert reset["kind"] == name and reset["rebuilds"]
            if name in ("failover", "weekly_start"):
                assert reset["sequence_resets"]
        elif name == "session_change":
            (reset,) = m["resets"]
            assert reset["trade_date_after"] == reset["trade_date_before"] + 1
        elif name in ("edge", "rare", "schema"):
            kinds = {"edge": ["mass_cancel", "mass_cancel", "sweep"],
                     "rare": ["trade_bust", "news", "news"],
                     "schema": ["extend", "news", "unknown"]}[name]
            assert [i["kind"] for i in m["injections"]] == kinds
            for inj in m["injections"]:
                placed = [v for k, v in inj.items() if k.endswith("placements")]
                assert placed and all(placed), (name, inj)
        elif name == "malformed":
            assert len(m["faults"]) == 6
        else:
            assert name == "clean" and not m["missing"]["A"] and not m["resets"]


def test_cli_and_library_give_the_same_bytes(built, tmp_path):
    out, _ = built
    settings = config.load(TINY)
    lib = tmp_path / "lib"
    main.build_set(settings.run_config(lib, settings.sets.small_messages), "small")
    main.build_set(settings.run_config(lib, settings.sets.burst_messages,
                                       spike_every_ns=settings.sets.burst_spike_every_ns), "burst")
    for name in main.SCENARIOS:
        main.run_scenario(settings.run_config(lib / "scenarios", settings.sets.scenario_messages),
                          name)
    pairs = [(out / s, lib / s) for s in ("small", "burst")] + \
        [(out / "scenarios" / n, lib / "scenarios" / n) for n in main.SCENARIOS]
    for a, b in pairs:
        for f in FILES - {"provenance.json"}:
            assert filecmp.cmp(a / f, b / f, shallow=False), (a, f)


def test_seed_and_messages_override_every_set(tmp_path):
    argv = ["build", "--config", str(TINY), "--out", str(tmp_path), "--seed", "5",
            "--messages", "300", "--sets", "small,burst"]
    assert cli.main_cli(argv) == 0
    for name in ("small", "burst"):
        m = manifest(tmp_path / name)
        prov = json.loads((tmp_path / name / "provenance.json").read_text())
        assert (m["seed"], m["target_messages"]) == (5, 300)
        assert (prov["seed"], prov["target_messages"]) == (5, 300)
    assert not (tmp_path / "large").exists() and not (tmp_path / "scenarios").exists()


def test_provenance_is_complete(built):
    out, argv = built
    text = TINY.read_text()
    git_dir = Path(__file__).resolve().parent
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_dir, capture_output=True,
                          text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=git_dir,
                                capture_output=True, text=True).stdout.strip())
    spec = sorted((git_dir.parents[1] / "spec").glob("*.xml"))
    for folder in folders(out):
        p = json.loads((folder / "provenance.json").read_text())
        assert p["config"] == {"path": str(TINY), "sha256": hashlib.sha256(
            TINY.read_bytes()).hexdigest(), "text": text}
        assert p["generator"] == {"git_commit": head, "git_dirty": dirty}
        assert p["schema"]["layouts_generated_from_sha256"] == layouts.XML_SHA256
        assert p["schema"]["spec_files"] == [
            {"path": str(f), "sha256": hashlib.sha256(f.read_bytes()).hexdigest()} for f in spec]
        assert spec and p["schema"]["spec_files"][0]["sha256"] == layouts.XML_SHA256
        assert p["command"] == ["python", "-m", "b3gen", *argv]
        assert p["seed"] == 1 and p["target_messages"] == manifest(folder)["target_messages"]
        assert p["set"] in ("small", "large", "burst", "scenarios")
        assert p["scenario"] == (folder.name if p["set"] == "scenarios" else None)
        assert p["python"] and p["started_utc"]


@pytest.mark.parametrize("argv,message", [
    (["--config", "/nonexistent.toml"], "cannot read"),
    (["--sets", "small,huge"], "unknown set ['huge']"),
    (["--scenarios", "loss,flood", "--sets", "scenarios"], "unknown scenario ['flood']"),
    (["--messages", "0"], "--messages must be > 0"),
    (["--sets", "scenarios", "--scenarios", "edge", "--messages", "200"],
     "raise the message target"),
])
def test_cli_errors_exit_2_with_one_line(argv, message, tmp_path):
    run = subprocess.run([sys.executable, "-m", "b3gen", "build", "--config", str(TINY),
                          "--out", str(tmp_path), *argv],
                         capture_output=True, text=True, cwd=Path(__file__).parents[1])
    assert run.returncode == 2
    assert run.stderr.startswith("error: ") and message in run.stderr, run.stderr
    assert "Traceback" not in run.stderr


def test_bad_config_through_the_cli(tmp_path):
    broken = tmp_path / "broken.toml"
    broken.write_text(TINY.read_text().replace("tick = 50_000", "tick = \"x\"", 1))
    run = subprocess.run([sys.executable, "-m", "b3gen", "build", "--config", str(broken),
                          "--out", str(tmp_path / "o")],
                         capture_output=True, text=True, cwd=Path(__file__).parents[1])
    assert run.returncode == 2
    assert f"{broken}: instruments[0].tick: expected int, got str ('x')" in run.stderr


def test_streamed_files_equal_the_in_memory_writer(tmp_path):
    a = main.run(main.default_config(seed=1, target_messages=20_000, out_dir=tmp_path / "s"))
    b = main.run(main.default_config(seed=1, target_messages=20_000, out_dir=tmp_path / "m"),
                 stream=False)
    assert a.messages == b.messages and a.packets == b.packets
    for f in ("feedA.pcap", "intent.jsonl", "book_key.jsonl"):
        assert filecmp.cmp(tmp_path / "s" / f, tmp_path / "m" / f, shallow=False), f
