"""Shared test setup: tools/ on the path, fixture locations, generated data.

Generated data lives in data/ at the repo root (make data), or wherever
B3_DATA points. Tests that need it fail, never skip, when it holds no sets:
a missing data folder must not let a data test pass silently.
"""

import os
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(TOOLS))


NO_DATA = "no generated sets in {path}: run `make data` (or set B3_DATA to a generator output folder)"


def data_dir():
    """The generator output folder: $B3_DATA, else data/ at the repo root."""
    env = os.environ.get("B3_DATA")
    path = Path(env) if env else REPO / "data"
    return path if path.is_absolute() else REPO / path


def generated_sets():
    """Every folder holding an intent.jsonl, as (name, path)."""
    root = data_dir()
    if not root.is_dir():
        return []
    return sorted((str(p.parent.relative_to(root)), p.parent)
                  for p in root.rglob("intent.jsonl"))


@pytest.fixture
def needs_data():
    """The data folder, or a test failure saying how to make it."""
    if not generated_sets():
        pytest.fail(NO_DATA.format(path=data_dir()), pytrace=False)
    return data_dir()
