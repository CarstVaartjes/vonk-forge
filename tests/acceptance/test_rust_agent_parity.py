"""Release-gate map for the recipe-native GPU node agent."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _rust_claim_capabilities() -> tuple[str, ...]:
    """Ask the compiled agent for the exact list used by its claim lane."""
    if sys.platform != "linux":
        pytest.skip("the production agent binary is Linux-only; run this gate in CI")
    result = subprocess.run(
        [
            "cargo",
            "run",
            "--quiet",
            "--locked",
            "-p",
            "vonk-agent",
            "--",
            "capabilities",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    capabilities = json.loads(result.stdout)
    assert isinstance(capabilities, list)
    assert all(isinstance(value, str) for value in capabilities)
    return tuple(capabilities)


def _controller_known_capabilities() -> frozenset[str]:
    """Evaluate the small, literal capability set in ``agent_jobs.py``.

    Importing the Controller here would make this package-level release gate
    depend on SQLAlchemy and its database drivers.  A deliberately narrow AST
    evaluator keeps the test dependency-free while still following the source
    declarations (including ``AgentOperation.*.value`` references).
    """
    protocol = ast.parse(_source("agent_protocol/src/vonk_agent_protocol/contracts.py"))
    operation_values = {
        item.targets[0].id: item.value.value
        for node in protocol.body
        if isinstance(node, ast.ClassDef) and node.name == "AgentOperation"
        for item in node.body
        if isinstance(item, ast.Assign)
        and len(item.targets) == 1
        and isinstance(item.targets[0], ast.Name)
        and isinstance(item.value, ast.Constant)
        and isinstance(item.value.value, str)
    }
    source = ast.parse(_source("control/src/vonk_control/agent_jobs.py"))
    assignments: dict[str, ast.AST] = {}
    for node in source.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value

    def evaluate(node: ast.AST, seen: frozenset[str] = frozenset()) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name):
            assert node.id not in seen, f"cyclic capability declaration: {node.id}"
            assert node.id in assignments, f"unknown capability declaration: {node.id}"
            return evaluate(assignments[node.id], seen | {node.id})
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "value"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr in operation_values
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "AgentOperation"
        ):
            return {operation_values[node.value.attr]}
        if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
            values: set[str] = set()
            for element in node.elts:
                values.update(evaluate(element, seen))
            return values
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"frozenset", "set"}
            and len(node.args) == 1
        ):
            return evaluate(node.args[0], seen)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return evaluate(node.left, seen) | evaluate(node.right, seen)
        raise AssertionError(f"unsupported capability declaration: {ast.dump(node)}")

    return frozenset(evaluate(assignments["_KNOWN_CAPABILITIES"]))


def test_rust_claim_capabilities_have_a_safe_controller_intersection() -> None:
    """Ensure the required runtime contract exists on both sides.

    The Controller deliberately negotiates the intersection: a newer agent may
    advertise capabilities an older Controller does not know yet, and those
    optional capabilities are ignored for that session.  Only the required
    runtime capabilities must overlap; Controller-only orchestration remains
    an intentional boundary.
    """
    advertised = _rust_claim_capabilities()
    known = _controller_known_capabilities()
    assert advertised, "Rust agent must advertise at least one capability"
    required_overlap = {
        "agent.runtime.rust.v1",
        "runtime.vonk.v1",
    }
    assert required_overlap <= set(advertised) & known
    assert known - set(advertised) == {
        "node.probe",
        "release.install",
        "workload.health",
        "workload.prepare",
        "workload.start",
        "workload.stop",
        "workload.verify",
    }
