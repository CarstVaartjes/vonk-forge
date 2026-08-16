from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_root_tests_do_not_import_control_implementation() -> None:
    offenders = []
    for path in (ROOT / "tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "insert" or not isinstance(node.func.value, ast.Attribute):
                continue
            if node.func.value.attr != "path" or not isinstance(node.func.value.value, ast.Name):
                continue
            if node.func.value.value.id != "sys":
                continue
            if "control/src" in ast.unparse(node):
                offenders.append(path)
                break
    assert offenders == []


def test_prototype_model_authority_is_absent() -> None:
    forbidden = (
        ROOT / "config/workloads",
        ROOT / "config/cluster-profiles",
        ROOT / "config/profile-selectors.toml",
        ROOT / "locks/model-definitions.toml",
        ROOT / "inventory/reports/model-definitions.json",
        ROOT / "inventory/reports/accepted-cluster-profiles.json",
        ROOT / "inventory/reports/deepseek-ds4-operational.json",
        ROOT / "inventory/reports/deepseek-mia-operational.json",
        ROOT / "release/workloads/ds4-v0.5.3-spark-runtime.json",
    )
    assert [str(path.relative_to(ROOT)) for path in forbidden if path.exists()] == []


def test_only_native_v1_model_adapter_roots_remain() -> None:
    files = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "adapters").rglob("*")
        if path.is_file()
        and path.name != "__init__.py"
        and "__pycache__" not in path.parts
    }
    assert files
    assert all(
        path.startswith(
            ("adapters/deepseek/ds4/", "adapters/deepseek/mia-vllm/")
        )
        for path in files
    )
