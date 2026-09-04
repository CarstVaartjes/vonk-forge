from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "select-pytest-shard-files"


def _module():
    loader = importlib.machinery.SourceFileLoader("select_pytest_shard_files", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


def test_shards_keep_files_whole_and_balance_collected_case_counts() -> None:
    module = _module()
    node_ids = [
        *(f"tests/slow.py::test_{index}" for index in range(5)),
        *(f"tests/medium.py::test_{index}" for index in range(3)),
        *(f"tests/small_{file}.py::test_one" for file in range(4)),
    ]

    shards = [
        module.select_files(node_ids, index=index, total=3) for index in range(3)
    ]

    flattened = [path for shard in shards for path in shard]
    assert sorted(flattened) == sorted(set(flattened))
    assert "tests/slow.py" in shards[0]
    assert "tests/medium.py" in shards[1]


def test_shard_selection_validates_bounds_and_applies_prefix() -> None:
    module = _module()
    node_ids = ["tests/a.py::test_a", "tests/b.py::test_b"]

    assert module.select_files(node_ids, index=0, total=2, prefix="control/") == [
        "control/tests/a.py"
    ]
    with pytest.raises(ValueError, match="within"):
        module.select_files(node_ids, index=2, total=2)
