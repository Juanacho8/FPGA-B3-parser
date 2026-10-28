#!/usr/bin/env python3
"""make_goldens: port 1 golden files for the RTL, one folder per generator set.

What it does:
  For every generator set under a data folder, writes DATA/golden/<set>/:
    expected_feedA.bin   tools/intent_to_events.py (the answer key), Feed A's order
    expected_feedB.bin   the same for Feed B, in Feed B's own packet order
                         (tools/b3feed.py reads it from the pcap)
    cpp_feedA.bin        b3_decode --events on feedA.pcap
    cpp_feedB.bin        b3_decode --events on feedB.pcap
    provenance.json      repo commit, the set's generator provenance (its
                         SHA-256 and recorded generator commit), the build
                         parameters, and the SHA-256 of every file above
  It refuses to write a set whose C++ files differ from the expected ones
  (make events-compare says where). A set in b3intent.EXPECTED_FAILURES (the
  reorder scenario, until the reorder window exists) keeps only
  expected_feedA.bin, in the window's release order (tools/b3intent.py,
  without capture times: the pcap's order is not the window's), with the
  reason in its provenance; it fails as XPASS if the C++ matches.
  data/ is gitignored; the goldens are reproducible with make data && make goldens.

What it must not do:
  Import b3gen, or write a golden that does not match its answer key.

Usage:
  python3 tools/make_goldens.py --cpp cpp_baseline/build/release/b3_decode DATA_DIR
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import b3contract as c   # noqa: E402
import events_compare     # noqa: E402  (the same instrument table as events-compare)
import b3intent           # noqa: E402
import b3replay           # noqa: E402
import intent_to_events   # noqa: E402
import b3intent          # noqa: E402


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def repo_commit():
    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True,
                            text=True, check=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True, check=True).stdout.strip())
    return {"commit": commit, "dirty": dirty}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data", help="generator output folder (make data)")
    ap.add_argument("--cpp", required=True, help="path to b3_decode")
    args = ap.parse_args(argv)

    root = Path(args.data)
    sets = sorted(p.parent for p in root.rglob("intent.jsonl") if "golden" not in p.parts)
    if not sets:
        print(f"error: no generator sets under {root}: run make data", file=sys.stderr)
        return 2
    commit = repo_commit()
    failed = xfailed = 0
    for folder in sets:
        name = folder.relative_to(root)
        out = root / "golden" / name
        out.mkdir(parents=True, exist_ok=True)
        files = {}
        manifest = b3intent.load_manifest(folder / "intent.jsonl")
        reason = b3intent.expected_failure(manifest)
        table = b3replay.read_table(events_compare.table_path())
        for feed in ("A", "B"):
            if not (folder / f"feed{feed}.pcap").is_file():
                continue
            try:
                records = intent_to_events.convert_feed(folder / "intent.jsonl", manifest, feed,
                                                        folder / f"feed{feed}.pcap", table)
            except b3replay.Broken as e:
                if not reason:
                    raise
                if feed != "A":
                    print(f"XFAIL {name}: expected_feed{feed}.bin not written ({reason}: {e})")
                    continue
                # Feed A of an expected failure: the window's release order, no times.
                rows = (r for _, r in b3intent.feed_rows(folder / "intent.jsonl", manifest, "A"))
                records = list(intent_to_events.convert(rows, b3replay.Replay(table, manifest, "A")))
            path = out / f"expected_feed{feed}.bin"
            path.write_bytes(c.events_header().pack() + b"".join(e.pack() for e in records))
            files[path.name] = path
        for feed in ("A", "B"):
            pcap = folder / f"feed{feed}.pcap"
            if not pcap.is_file():
                print(f"SKIPPED {name}/{pcap.name}: no such file in this set")
                continue
            path = out / f"cpp_feed{feed}.bin"
            subprocess.run([str(args.cpp), "--instruments", events_compare.table_path(), "--events", str(path),
                            str(pcap)], check=True,
                           stdout=subprocess.DEVNULL)
            files[path.name] = path
        matches = all(f"cpp_feed{f}.bin" in files and f"expected_feed{f}.bin" in files and
                      files[f"cpp_feed{f}.bin"].read_bytes() == files[f"expected_feed{f}.bin"].read_bytes()
                      for f in ("A", "B") if (folder / f"feed{f}.pcap").is_file())
        if reason and matches:
            print(f"XPASS {name}: cpp_feedA.bin matches, but is expected to fail ({reason}); "
                  f"remove it from b3intent.EXPECTED_FAILURES")
            for f in files.values():
                f.unlink()
            failed += 1
            continue
        if reason:
            for n in ("cpp_feedA.bin", "cpp_feedB.bin"):
                if n in files:
                    files.pop(n).unlink()
            xfailed += 1
        elif not matches:
            print(f"FAIL {name}: a cpp_feed*.bin differs from its expected file (run make events-compare)")
            for f in files.values():
                f.unlink()
            failed += 1
            continue
        header = c.events_header()
        gen = folder / "provenance.json"
        gen_info = json.loads(gen.read_text()) if gen.is_file() else {}
        provenance = {
            "set": str(name),
            "repo": commit,
            "generator_provenance": {"sha256": sha256(gen) if gen.is_file() else None,
                                     "generator": gen_info.get("generator"),
                                     "seed": gen_info.get("seed")},
            "build": {"published_depth": header.published_depth, "tracked_depth": header.tracked_depth,
                      "max_instruments": header.max_instruments,
                      "book": "uncapped" if header.tracked_depth == 0 else "capped"},
            "files": {n: {"sha256": sha256(p), "bytes": p.stat().st_size} for n, p in sorted(files.items())},
        }
        if reason:
            provenance["expected_failure"] = f"{reason}: no cpp_feed*.bin, the C++ does not match yet"
        (out / "provenance.json").write_text(json.dumps(provenance, indent=1) + "\n")
        print(f"{'XFAIL' if reason else 'OK'} {name}: {', '.join(sorted(files))}"
              + (f" ({reason})" if reason else ""))
    print(f"{len(sets) - failed}/{len(sets)} sets written under {root / 'golden'}, "
          f"{xfailed} expected failures (expected file only)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
