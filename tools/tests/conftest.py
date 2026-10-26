"""Shared test setup: tools/ on the path, fixture locations, generated data.

B3_DATA points at a generator output folder (python -m b3gen build --out DIR).
Tests that need generated sets skip when it is not set and feed_generator/data
does not exist.
"""

import os
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(TOOLS))


def data_dir():
    env = os.environ.get("B3_DATA")
    path = Path(env) if env else REPO / "feed_generator" / "data"
    return path if path.is_dir() else None


def generated_sets():
    """Every folder holding an intent.jsonl, as (name, path)."""
    root = data_dir()
    if root is None:
        return []
    return sorted((str(p.parent.relative_to(root)), p.parent)
                  for p in root.rglob("intent.jsonl"))


@pytest.fixture
def needs_data():
    if data_dir() is None:
        pytest.skip("no generated data (set B3_DATA or build into feed_generator/data)")
    return data_dir()
