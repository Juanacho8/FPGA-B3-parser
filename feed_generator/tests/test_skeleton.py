import importlib

import pytest

MODULES = [
    "flow", "engine", "publisher", "packet", "pcap",
    "layouts", "encode", "golden", "scenarios", "config", "main",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports_and_has_docstring(name):
    module = importlib.import_module(f"b3gen.{name}")
    assert module.__doc__ and module.__doc__.strip()
