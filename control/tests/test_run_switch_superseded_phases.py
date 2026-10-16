"""Every Run/Switch phase call that binds the load's intent re-plans when superseded.

A newer workload intent refuses such a call with ``SUPERSEDED``. Only inside
``with _replans_when_superseded():`` does that refusal go back to the
Run/Switch, which reads the newer intent and ends the load ``superseded``;
anywhere else it reaches the generic phase handler and ends the load failed.
The calls are the ones that retire or observe older work under the intent
(``reconcile_superseded_unissued``, ``_observe_older_issued``) and every call
that passes ``workload_intent_ordinal=``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PHASES = (
    Path(__file__).resolve().parents[1]
    / "src/vonk_control/run_switch_operations/executor_phases.py"
)
_RETIRE_OR_OBSERVE = {"reconcile_superseded_unissued", "_observe_older_issued"}


def _binds_intent(call: ast.Call) -> bool:
    callee = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(call.func)
        if isinstance(node, ast.Attribute | ast.Name)
    }
    return bool(callee & _RETIRE_OR_OBSERVE) or any(
        keyword.arg == "workload_intent_ordinal" for keyword in call.keywords
    )


def _replans(node: ast.With) -> bool:
    return any(
        isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Name)
        and item.context_expr.func.id == "_replans_when_superseded"
        for item in node.items
    )


def unwrapped_intent_calls(source: str) -> list[int]:
    """Lines of intent-binding calls outside ``with _replans_when_superseded()``."""

    lines: list[int] = []

    def visit(node: ast.AST, wrapped: bool) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            wrapped = False
        if isinstance(node, ast.With) and _replans(node):
            wrapped = True
        if isinstance(node, ast.Call) and _binds_intent(node) and not wrapped:
            lines.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, wrapped)

    visit(ast.parse(source), False)
    return lines


def test_every_intent_binding_phase_call_replans_when_superseded() -> None:
    assert unwrapped_intent_calls(PHASES.read_text()) == []


_WRAPPED = """
def phase(service, ordinal, job):
    with _replans_when_superseded():
        service._lifecycle.reconcile_superseded_unissued("recipe.install", "i", ordinal)
        service._observe_older_issued("recipe.install", "i", ordinal)
    try:
        with service._sessions() as session, _replans_when_superseded():
            value = (service.start if job else service.activate)(
                session, workload_intent_ordinal=ordinal
            )
    except RuntimeError:
        raise
    return value
"""


@pytest.mark.parametrize(
    "unwrapped",
    [
        'service._lifecycle.reconcile_superseded_unissued("recipe.stop", "r", ordinal)',
        'service._observe_older_issued("recipe.stop", "r", ordinal)',
        "start_installation('i', workload_intent_ordinal=ordinal)",
        "(service.start if job else service.activate)(workload_intent_ordinal=ordinal)",
    ],
)
def test_an_intent_call_moved_out_of_the_replan_fails_the_guard(
    unwrapped: str,
) -> None:
    assert unwrapped_intent_calls(_WRAPPED) == []
    moved = _WRAPPED.replace("    return value", f"    {unwrapped}\n    return value")
    assert len(unwrapped_intent_calls(moved)) == 1


def test_a_nested_helper_does_not_inherit_the_callers_replan() -> None:
    source = """
def phase(service, ordinal):
    with _replans_when_superseded():
        def later():
            return service.uninstall("i", workload_intent_ordinal=ordinal)
        return later
"""
    assert unwrapped_intent_calls(source) == [5]
