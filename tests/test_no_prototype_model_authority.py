from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_root_tests_do_not_import_control_implementation() -> None:
    offenders = []
    for path in (ROOT / "tests").rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports_control = any(
            (
                isinstance(node, ast.Import)
                and any(alias.name == "vonk_control" or alias.name.startswith("vonk_control.") for alias in node.names)
            )
            or (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and (node.module == "vonk_control" or node.module.startswith("vonk_control."))
            )
            for node in ast.walk(tree)
        )
        if imports_control:
            offenders.append(path)
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"insert", "append"} or not isinstance(node.func.value, ast.Attribute):
                continue
            if node.func.value.attr != "path" or not isinstance(node.func.value.value, ast.Name):
                continue
            if node.func.value.value.id != "sys":
                continue
            source = ast.unparse(node)
            if "control" in source and "src" in source:
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


def test_retired_profile_controller_modules_are_absent() -> None:
    forbidden = (
        "src/cluster_profiles/admission.py",
        "src/cluster_profiles/backend.py",
        "src/cluster_profiles/catalog.py",
        "src/cluster_profiles/contracts.py",
        "src/cluster_profiles/health.py",
        "src/cluster_profiles/legacy_cli.py",
        "src/cluster_profiles/profile_compat.py",
        "src/cluster_profiles/state.py",
        "src/cluster_profiles/switcher.py",
        "src/cluster_profiles/fleet/legacy.py",
        "src/cluster_profiles/workload_packages/legacy.py",
        "src/cluster_profiles/schemas/accepted-cluster-profiles.schema.json",
        "src/cluster_profiles/schemas/model-definitions.schema.json",
        "src/cluster_profiles/schemas/node-health-raw.schema.json",
        "src/cluster_profiles/schemas/node-health.schema.json",
    )
    assert [path for path in forbidden if (ROOT / path).exists()] == []
