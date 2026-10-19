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


def _backstop_gaps(adapters: set[str], models: set[str], kinds: set[str]):
    tree = ast.parse((SOURCE / "wait_redrive.py").read_text())
    registries = {
        ast.unparse(target): node.value
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
    }
    adapter_map = registries["ADAPTER_WAIT_KINDS"]
    owner_map = registries["WAIT_MODELS"]
    assert isinstance(adapter_map, ast.Dict) and isinstance(owner_map, ast.Dict)
    return (
        adapters - {ast.literal_eval(key) for key in adapter_map.keys if key},
        models - {ast.unparse(value) for value in owner_map.values},
        kinds - {ast.unparse(key).rsplit(".", 1)[-1] for key in owner_map.keys if key},
    )


def test_every_persisted_wait_owner_is_redriven_by_the_worker() -> None:
    """A new ORM wait owner, adapter or canonical kind cannot remain traffic-only."""
    classes = [
        node
        for path in SOURCE.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ClassDef)
        and not any("Protocol" in ast.unparse(base) for base in node.bases)
    ]
    adapters = {
        node.name
        for node in classes
        if node.name.endswith("Adapter")
        and any(
            isinstance(member, ast.FunctionDef) and member.name == "adopt"
            for member in node.body
        )
    }
    models = {
        node.name
        for node in classes
        if any(
            isinstance(member, ast.AnnAssign)
            and ast.unparse(member.target) == "wait_evidence"
            for member in node.body
        )
    }
    vocabulary = ast.parse(
        (
            ROOT / "agent_protocol/src/vonk_agent_protocol/lifecycle_vocabulary.py"
        ).read_text()
    )
    kind_enum = next(
        node
        for node in vocabulary.body
        if isinstance(node, ast.ClassDef) and node.name == "WaitOwnerKind"
    )
    kinds = {
        target.id
        for member in kind_enum.body
        if isinstance(member, ast.Assign)
        for target in member.targets
        if isinstance(target, ast.Name)
    }
    assert adapters and models and kinds
    assert _backstop_gaps(adapters, models, kinds) == (set(), set(), set())


def test_backstop_gate_rejects_an_added_owner_and_adapter() -> None:
    """Mutation witness: custody alone cannot satisfy the redrive guard."""
    assert _backstop_gaps({"NewAdapter"}, {"NewWaitOwner"}, {"NEW_KIND"}) == (
        {"NewAdapter"},
        {"NewWaitOwner"},
        {"NEW_KIND"},
    )
