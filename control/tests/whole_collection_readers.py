"""A request path asks about its target; only a collector reads the whole collection.

P17, "ask the target, not the world": a check about one item answers with
lookups keyed by that item. These readers walk every profile, installation,
plan or day of jobs, which is right for a collector that decides over all of
them and wrong for a request or per-event handler that wants one answer. Added
lines outside the collector modules may not call them, and may not select every
row of a table that grows with history. The runtime half is
``test_target_budget.py``.
"""

from __future__ import annotations

import ast

COLLECTOR_MODULES = (
    "control/src/vonk_control/catalog_revision_collection.py",
    "control/src/vonk_control/terminal_history_collection.py",
    "control/src/vonk_control/unused_storage_collection/",
)
# Readers that load or parse the whole collection to answer for any target.
WHOLE_COLLECTION_READERS = frozenset(
    {
        "live_tokens",
        "operation_tokens",
        "streamed_tokens",
        "pinned_by_heads",
        "_profile_pointers",
        "runtime_image_reference_findings",
        "runtime_image_reference_reasons",
        "model_set_reference_findings",
        "model_set_reference_reasons",
    }
)
# ``read`` is only a whole-collection reader on the evidence classes.
EVIDENCE_CLASSES = frozenset({"_Evidence", "_CollectorEvidence"})
# Tables that grow with history, the catalog or the fleet.
GROWING_TABLES = frozenset(
    {
        "RecipeInstallation",
        "FleetProfile",
        "FleetProfileApplication",
        "Job",
        "ModelCacheSet",
        "CatalogDocumentRevision",
    }
)
NARROWING = frozenset({"where", "filter", "filter_by", "limit", "join", "outerjoin"})


def _tail(node: ast.AST) -> str:
    return (
        node.attr
        if isinstance(node, ast.Attribute)
        else node.id
        if isinstance(node, ast.Name)
        else ""
    )


def is_collector(path: str) -> bool:
    return path.startswith(COLLECTOR_MODULES)


def _reader_call(call: ast.Call) -> bool:
    name = _tail(call.func)
    if name in WHOLE_COLLECTION_READERS:
        return True
    return (
        name == "read"
        and isinstance(call.func, ast.Attribute)
        and _tail(call.func.value) in EVIDENCE_CLASSES
    )


def _unfiltered_select(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    """``select(Model)`` of a growing table with nothing narrowing it."""
    if not (
        _tail(call.func) == "select"
        and len(call.args) == 1
        and not call.keywords
        and _tail(call.args[0]) in GROWING_TABLES
    ):
        return False
    chain: ast.AST = call
    while isinstance(parents.get(chain), ast.Attribute) and isinstance(
        parents.get(parents[chain]), ast.Call
    ):
        chain = parents[parents[chain]]
        if _tail(getattr(chain, "func", chain)) in NARROWING:
            return False
    # ``statement = select(Model)`` narrowed by a later ``statement.where(...)``.
    assignment = parents.get(chain)
    if isinstance(assignment, ast.Assign):
        names = {_tail(target) for target in assignment.targets}
        function = chain
        while function in parents and not isinstance(
            function, ast.FunctionDef | ast.AsyncFunctionDef | ast.Module
        ):
            function = parents[function]
        return not any(
            isinstance(other, ast.Call)
            and _tail(other.func) in NARROWING
            and isinstance(other.func, ast.Attribute)
            and _tail(other.func.value) in names
            for other in ast.walk(function)
        )
    return True


def scan_source(source: str, *, path: str) -> list[tuple[int, str]]:
    """Lines that read a whole collection outside a collector."""
    if is_collector(path):
        return []
    tree = ast.parse(source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _reader_call(node):
            hits.append((node.lineno, "whole-collection reader outside a collector"))
        elif _unfiltered_select(node, parents):
            hits.append((node.lineno, "select of every row of a growing table"))
    return hits
