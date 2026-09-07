from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "select-pytest-shard-files"


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


def test_historical_durations_balance_files_and_leave_new_files_deterministic() -> None:
    module = _module()
    node_ids = [
        *(f"tests/slow.py::test_{index}" for index in range(2)),
        "tests/medium.py::test_one",
        "tests/new.py::test_one",
    ]
    durations = {"tests/slow.py": 10.0, "tests/medium.py": 3.0}

    shards = [
        module.select_files(node_ids, index=index, total=2, durations=durations)
        for index in range(2)
    ]

    assert shards == [["tests/slow.py"], ["tests/medium.py", "tests/new.py"]]


def test_duration_loader_rejects_unknown_schema_and_stale_data(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "durations.json"
    old = datetime.now(UTC) - timedelta(hours=3)
    path.write_text(
        json.dumps(
            {
                "schema_version": 99,
                "generated_at": old.isoformat(),
                "files": {"tests/a.py": 4},
            }
        )
    )
    assert module.load_durations(path) == {}
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": old.isoformat(),
                "files": {"tests/a.py": 4},
            }
        )
    )
    assert module.load_durations(path, max_age_hours=2) == {}
