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


def _violations(
    source: str, path: str, *, constructors_only: bool = False
) -> list[str]:
    tree = ast.parse(source)
    errors: list[str] = []
    retired_clocks = {"retry_after_at", "admission_retry_at", "observation_due_at"}
    retired_constructor_clocks = {
        "RecipeRun": {"route_next_attempt_at"},
        "AgentCertificate": {"ca_next_attempt_at"},
        "RoutePublicationOwner": {"reconciliation_next_at"},
        "AgentIssuedCertificateRevocation": {"next_attempt_at"},
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            retired = retired_constructor_clocks.get(
                ast.unparse(node.func).split(".")[-1], set()
            )
            if any(keyword.arg in retired for keyword in node.keywords):
                errors.append(f"{path}:{node.lineno}: retired owner clock")
        if constructors_only:
            continue
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
        if path != "persisted_waits.py" and isinstance(
            node, ast.Assign | ast.AnnAssign | ast.AugAssign
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in {
                    "next_action_at",
                    "wait_evidence",
                }:
                    errors.append(f"{path}:{node.lineno}: direct wait writer")
        if isinstance(node, ast.Dict) and any(
            isinstance(key, ast.Constant) and key.value in retired_clocks
            for key in node.keys
        ):
            errors.append(f"{path}:{node.lineno}: parallel JSON wait clock")
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in retired_clocks
        ):
            errors.append(f"{path}:{node.lineno}: parallel JSON wait clock")
    return errors


def test_all_lifecycle_adapters_and_durable_clock_writers_use_wait_custody() -> None:
    """A future adapter or direct clock write cannot evade an added-line gate."""
    errors = [
        error
        for file in [*SOURCE.rglob("*.py"), *(ROOT / "control/tests").rglob("*.py")]
        for error in _violations(
            file.read_text(),
            file.relative_to(SOURCE).as_posix()
            if file.is_relative_to(SOURCE)
            else file.relative_to(ROOT).as_posix(),
            constructors_only=not file.is_relative_to(SOURCE),
        )
    ]
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize(
    "source",
    [
        "class NewAdapter:\n def adopt(self, row): return row\n",
        "def defer(row, due):\n row.next_action_at = due\n",
        "def defer(row, evidence):\n row.wait_evidence = evidence\n",
        "def defer(row, due):\n row.next_action_at: object = due\n",
        "def defer(row, due):\n row.progress['retry_after_at'] = due\n",
        "def defer(row, due):\n return {'observation_due_at': due}\n",
        "RecipeRun(route_next_attempt_at=None)",
        "AgentCertificate(ca_next_attempt_at=None)",
    ],
)
def test_owner_gate_rejects_uncovered_new_writer(source: str) -> None:
    assert _violations(source, "new_owner.py")
