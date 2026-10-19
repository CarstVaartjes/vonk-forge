"""Whole-tree ownership gate: a new adapter or clock writer must use custody."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "control/src/vonk_control"
WRITERS = frozenset(
    {"lifecycle_wait", "schedule_wait", "initialize_wait", "write_wait"}
)


def _violations(source: str, path: str) -> list[str]:
    tree = ast.parse(source)
    errors: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name.endswith("Adapter")
            and not any("Protocol" in ast.unparse(base) for base in node.bases)
            and any(
                isinstance(m, ast.FunctionDef) and m.name == "adopt" for m in node.body
            )
        ):
            calls = {
                ast.unparse(call.func).split(".")[-1]
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            }
            if not calls.intersection(WRITERS):
                errors.append(
                    f"{path}:{node.lineno}: {node.name} bypasses persisted waits"
                )
        if path != "persisted_waits.py" and isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in {
                    "next_action_at",
                    "wait_evidence",
                }:
                    errors.append(f"{path}:{node.lineno}: direct wait writer")
    return errors


def test_all_lifecycle_adapters_and_durable_clock_writers_use_wait_custody() -> None:
    """A future adapter or direct clock write cannot evade an added-line gate."""
    errors = [
        error
        for file in SOURCE.rglob("*.py")
        for error in _violations(file.read_text(), file.relative_to(SOURCE).as_posix())
    ]
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize(
    "source",
    [
        "class NewAdapter:\n def adopt(self, row): return row\n",
        "def defer(row, due):\n row.next_action_at = due\n",
        "def defer(row, evidence):\n row.wait_evidence = evidence\n",
    ],
)
def test_owner_gate_rejects_uncovered_new_writer(source: str) -> None:
    assert _violations(source, "new_owner.py")
