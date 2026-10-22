"""Shared fixtures: every scenario run once per test session."""

import pytest

from b3gen import main


def scenario_config(out_dir, **overrides):
    return main.default_config(seed=1, target_messages=6000, out_dir=out_dir,
                               book_key_every=50, **overrides)


@pytest.fixture(scope="session")
def runs(tmp_path_factory):
    out = tmp_path_factory.mktemp("scenarios")
    return {name: main.run_scenario(scenario_config(out), name) for name in main.SCENARIOS}
