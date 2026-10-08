"""Test doubles must patch the lookup owner, never Run/Switch facade exports."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FACADE = "vonk_control.run_switch_operations"


def facade_patch_lines(source: str) -> list[int]:
    """Find direct facade patches, including import aliases and dotted strings.

    Patching an exported class's method is valid: consumers share that class.
    Replacing a name on the facade changes only its re-exported binding.
    """
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name.split(".")[0]] = (
                    name.name if name.asname else name.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            for name in node.names:
                aliases[name.asname or name.name] = f"{node.module}.{name.name}"

    def resolve(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{resolve(node.value)}.{node.attr}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return ""

    # Simple object aliases are common in test helpers.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and (value := resolve(node.value)):
                    aliases[target.id] = value

    failures = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = resolve(node.func)
        method = name.rsplit(".", 1)[-1]
        if method in {"setattr", "delattr"} or name.endswith("patch.object"):
            target = resolve(node.args[0])
            # String form: monkeypatch.setattr("module.attribute", value).
            owner = (
                target.rsplit(".", 1)[0]
                if isinstance(node.args[0], ast.Constant)
                else target
            )
        elif method == "patch":
            owner = resolve(node.args[0]).rsplit(".", 1)[0]
        else:
            continue
        if owner == FACADE:
            failures.append(node.lineno)
    return failures


def test_control_tests_never_patch_run_switch_facade_reexports() -> None:
    failures = {
        str(path.relative_to(ROOT)): lines
        for path in sorted((ROOT / "control/tests").rglob("*.py"))
        if (lines := facade_patch_lines(path.read_text(encoding="utf-8")))
    }
    assert not failures, f"Patch the submodule that looks up the name: {failures}"


@pytest.mark.parametrize(
    "source",
    [
        'from vonk_control import run_switch_operations as owner\nfault.setattr(owner, "helper", fake)',
        'import vonk_control.run_switch_operations as owner\nmonkeypatch.setattr(owner, "helper", fake)',
        'import vonk_control.run_switch_operations\nmonkeypatch.setattr(vonk_control.run_switch_operations, "helper", fake)',
        'monkeypatch.setattr("vonk_control.run_switch_operations.helper", fake)',
        'from unittest.mock import patch\npatch("vonk_control.run_switch_operations.helper")',
        'from unittest.mock import patch as replace\nreplace("vonk_control.run_switch_operations.helper")',
        'from unittest.mock import patch\nfrom vonk_control import run_switch_operations as owner\npatch.object(owner, "helper")',
        'from vonk_control import run_switch_operations\nowner = run_switch_operations\nmonkeypatch.setattr(owner, "helper", fake, raising=False)',
    ],
)
def test_guard_rejects_ineffective_facade_patches(source: str) -> None:
    assert facade_patch_lines(source)


@pytest.mark.parametrize(
    "source",
    [
        'from vonk_control.run_switch_operations import advance as owner\nfault.setattr(owner, "_merge_progress_evidence", fake)',
        'monkeypatch.setattr("vonk_control.run_switch_operations.advance.helper", fake)',
        'from vonk_control.run_switch_operations import RunSwitchOperationService\nmonkeypatch.setattr(RunSwitchOperationService, "cancel", fake)',
        'from unittest.mock import patch\npatch("vonk_control.run_switch_operations.RunSwitchOperationService.cancel")',
    ],
)
def test_guard_accepts_lookup_owners_and_shared_class_methods(source: str) -> None:
    assert not facade_patch_lines(source)
