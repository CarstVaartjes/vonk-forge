"""Find a run or rank recorded ``failed`` without proof that its process is gone.

A ``failed`` run holds no claim: the ownership rule
(``release_inactive_run_claims_in_session``) frees its ports and memory, and no
plan stops it. So ``failed`` is a claim about the Spark, and only the Spark's
own word proves it. An effect that is unknown (a Stop that was not confirmed, a
Start without cleanup authority, a rank whose report is missing) is ``lost``:
stoppable, its claims held, observed until the Spark reports it gone::

    run.state = RunState.FAILED                       # forbidden: nothing proves it
    run.state = ended_run_state(run, nodes, now)      # FAILED only on proof

The proof is a fresh absence read in the same function: one of the absence
helpers below, or the ``process_running`` field of the Spark report being
applied. Whole tree, no allowances: every site must carry its own proof.
"""

from __future__ import annotations

import ast
from pathlib import Path

from .parsed_sources import parse_file

KIND = "run-failed-without-fresh-absence"
ROOT = Path(__file__).resolve().parents[2]
ROOTS = ("control/src", "src/cluster_profiles")
#: The reads that prove a rank's process gone on its Spark, now.
ABSENCE_READS = frozenset(
    {
        "_proves_fresh_absence",
        "run_node_reports_absent",
        "settle_absent_run_in_session",
        "settle_observed_absent_runs_in_session",
    }
)
#: The field of a Spark's run report that says whether the process runs.
REPORT_FIELD = "process_running"


def _is_failed_run_state(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "FAILED"
        and isinstance(node.value, ast.Name)
        and node.value.id == "RunState"
    )


def _own_nodes(function: ast.AST):
    """Nodes of ``function`` itself, not of functions nested in it."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            stack.extend(ast.iter_child_nodes(node))


def _assigns_failed_state(node: ast.AST) -> bool:
    if isinstance(node, ast.Assign):
        targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets, value = [node.target], node.value
    else:
        return False
    return any(
        isinstance(target, ast.Attribute) and target.attr == "state"
        for target in targets
    ) and any(_is_failed_run_state(part) for part in ast.walk(value))


def _reads_absence(function: ast.AST) -> bool:
    for node in _own_nodes(function):
        if isinstance(node, ast.Call):
            called = node.func
            name = (
                called.id
                if isinstance(called, ast.Name)
                else called.attr
                if isinstance(called, ast.Attribute)
                else ""
            )
            if name in ABSENCE_READS:
                return True
        if (
            isinstance(node, ast.Attribute)
            and node.attr == REPORT_FIELD
            and isinstance(node.ctx, ast.Load)
        ):
            return True
    return False


def unproven_failures(tree: ast.Module) -> list[tuple[str, int]]:
    """``(function, line)`` of each ``failed`` run or rank with no absence read."""
    sites: list[tuple[str, int]] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        assigned = [
            node.lineno
            for node in _own_nodes(function)
            if isinstance(node, ast.Assign | ast.AnnAssign)
            and _assigns_failed_state(node)
        ]
        if assigned and not _reads_absence(function):
            sites.extend((function.name, line) for line in assigned)
    return sorted(sites, key=lambda site: site[1])


def tree_sites() -> list[tuple[str, str, int]]:
    """``(path, function, line)`` of every unproven ``failed`` in the covered trees."""
    return [
        (path.relative_to(ROOT).as_posix(), function, line)
        for root in ROOTS
        for path in sorted((ROOT / root).rglob("*.py"))
        if "generated_control" not in path.parts and "__pycache__" not in path.parts
        for function, line in unproven_failures(parse_file(path).tree)
    ]
