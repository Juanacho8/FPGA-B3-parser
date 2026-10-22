"""Support: command line.

    python -m b3gen build [--config PATH] [--out DIR] [--seed N] [--messages N]
                          [--sets all|small,large,burst,scenarios]
                          [--scenarios NAME,...]

Builds the standard sets from one config file:

    DIR/small/            ~20k messages (RTL simulation)
    DIR/large/            ~1M messages
    DIR/burst/            spikes much more often (sets.burst_spike_every_ns)
    DIR/scenarios/NAME/   every scenario (main.SCENARIOS)

Each folder holds feedA.pcap, feedB.pcap, intent.jsonl, book_key.jsonl,
manifest.json and provenance.json. --seed and --messages override the config
for every set chosen (the malformed scenario keeps its fixed small size).
A bad config or a scenario that cannot fit its target exits with status 2
and a one-line message.

Must not: generate anything itself; it reads the config and calls main.
"""

import argparse
import datetime
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

from b3gen import config as config_mod
from b3gen import layouts, main, scenarios

SETS = ("small", "large", "burst", "scenarios")
REPO_SPEC = Path(__file__).resolve().parents[2] / "spec"


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m b3gen",
                                     description="B3 Binary UMDF feed generator")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build the standard sets from a config")
    build.add_argument("--config", default=str(config_mod.DEFAULT_PATH),
                       help="TOML config (default: feed_generator/config.toml)")
    build.add_argument("--out", default="data", help="output directory (default: data)")
    build.add_argument("--seed", type=int, help="override the config's seed")
    build.add_argument("--messages", type=int, help="message target for every set chosen")
    build.add_argument("--sets", default="all",
                       help="comma-separated: small, large, burst, scenarios (default: all)")
    build.add_argument("--scenarios", default="all",
                       help=f"comma-separated scenario names (default: all of "
                            f"{', '.join(main.SCENARIOS)})")
    return parser


def _choose(value, allowed, what):
    if value == "all":
        return list(allowed)
    chosen = [v.strip() for v in value.split(",") if v.strip()]
    unknown = [v for v in chosen if v not in allowed]
    if unknown or not chosen:
        raise ValueError(f"unknown {what} {unknown or value!r}; choose from {', '.join(allowed)}")
    return chosen


def main_cli(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    args = build_parser().parse_args(argv)
    try:
        sets = _choose(args.sets, SETS, "set")
        names = _choose(args.scenarios, main.SCENARIOS, "scenario")
        settings = config_mod.load(args.config)
        if args.messages is not None and args.messages <= 0:
            raise ValueError("--messages must be > 0")
        build(settings, Path(args.out), sets, names, seed=args.seed, messages=args.messages,
              command=["python", "-m", "b3gen", *argv])
    except (ValueError, config_mod.ConfigError, scenarios.ScenarioError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def build(settings, out, sets, scenario_names, *, seed=None, messages=None, command=None):
    """Build the chosen sets; returns {folder: manifest}."""
    s = settings.sets
    built = {}

    def provenance(folder, set_name, scenario, config):
        _write_provenance(folder, settings, set_name, scenario, config, command)

    for name in [n for n in ("small", "large", "burst") if n in sets]:
        target = messages or {"small": s.small_messages, "large": s.large_messages,
                              "burst": s.burst_messages}[name]
        config = settings.run_config(out, target, seed=seed,
                                     spike_every_ns=s.burst_spike_every_ns
                                     if name == "burst" else None)
        result = main.build_set(config, name)
        provenance(out / name, name, None, config)
        built[out / name] = result.manifest
    if "scenarios" in sets:
        target = messages or s.scenario_messages
        config = settings.run_config(out / "scenarios", target, seed=seed)
        for name in scenario_names:
            result = main.run_scenario(config, name)
            provenance(out / "scenarios" / name, "scenarios", name, result.run_config)
            built[out / "scenarios" / name] = result.manifest
    return built


def _git(*args):
    try:
        out = subprocess.run(["git", *args], cwd=Path(__file__).resolve().parent,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _write_provenance(folder, settings, set_name, scenario, config, command):
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    spec = sorted(REPO_SPEC.glob("*.xml")) if REPO_SPEC.is_dir() else []
    record = {
        "set": set_name,
        "scenario": scenario,
        "seed": config.seed,
        "target_messages": config.target_messages,
        "config": {"path": settings.path, "sha256": settings.sha256, "text": settings.text},
        "generator": {"git_commit": commit.strip() if commit else None,
                      "git_dirty": bool(status.strip()) if status is not None else None},
        "schema": {"layouts_generated_from_sha256": layouts.XML_SHA256,
                   "spec_files": [{"path": str(p), "sha256": hashlib.sha256(
                       p.read_bytes()).hexdigest()} for p in spec]},
        "command": command,
        "python": platform.python_version(),
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    Path(folder, "provenance.json").write_text(json.dumps(record, indent=1) + "\n",
                                               encoding="utf-8")
