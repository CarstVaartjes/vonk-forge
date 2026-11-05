"""A request path asks about its target; only a collector reads the whole collection.

P17, "ask the target, not the world": a check about one item answers with
lookups keyed by that item. These readers walk every profile, installation,
plan or day of jobs, which is right for a collector that decides over all of
them and wrong for a request or per-event handler that wants one answer. Added
lines outside the collector modules may not call them, and may not select every
row of a growing table anywhere in the Controller. The table set is
``request_path_scans.TABLES`` (one owner); ``request-scans`` keeps its stricter
rule for API and projection modules. The runtime half is
``test_target_budget.py``.
"""

from __future__ import annotations

import ast

from .request_path_scans import TABLES, is_background, is_narrowed

_STORAGE = "control/src/vonk_control/unused_storage_collection/"
# Files that only a collector pass runs. The directory also holds request entry
# points (``common.spark_eviction_capacity``, ``service.relief_for_spark``), so
# it is not exempt as a whole.
COLLECTOR_MODULES = (
    "control/src/vonk_control/catalog_revision_collection.py",
    "control/src/vonk_control/terminal_history_collection.py",
    f"{_STORAGE}removal.py",
    f"{_STORAGE}pressure.py",
)
# In ``service.py`` only the collector's own passes are exempt.
COLLECTOR_FUNCTIONS = {
    # The evidence readers and the reference scans the collector passes use;
    # the per-installation helpers beside them serve a node-keyed review too.
    f"{_STORAGE}evidence.py": frozenset({"read"}),
    f"{_STORAGE}references.py": frozenset(
        {
            "_profile_pointers",
            "_applied_revision_ids",
            "_model_removal_in_flight",
            "_latest_snapshots",
        }
    ),
    f"{_STORAGE}service.py": frozenset(
        {"tick", "collect", "redrive_installation", "redrive_artifact", "_expire"}
    ),
    # Controller startup fills the saved-profile projection once per start.
    "control/src/vonk_control/reference_facts.py": frozenset({"_fill_profile_recipes"}),
    # Both walk every saved profile by design: the image scan is itself a
    # whole-collection reader, and the storage refresh recomputes every set.
    "control/src/vonk_control/artifact_reference_scan/image_references.py": frozenset(
        {"runtime_image_reference_findings"}
    ),
    "control/src/vonk_control/model_cache/inventory.py": frozenset(
        {"_refresh_protection"}
    ),
    # Resolves each saved profile's recipe, as a download would; the saved
    # recipes are the protected work the budget lets grow linearly.
    "control/src/vonk_control/artifact_reference_scan/model_references.py": frozenset(
        {"model_set_reference_findings"}
    ),
}
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
        # Every saved profile's recipes when called without ``recipes``.
        "profiles_naming",
        # Wrappers that rebuild the whole evidence for one node.
        "spark_eviction_capacity",
        "relief_for_spark",
    }
)
# ``read`` is only a whole-collection reader on the evidence classes.
EVIDENCE_CLASSES = frozenset({"_Evidence", "_CollectorEvidence"})


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


def _non_terminal_only(call: ast.Call) -> bool:
    """``operation_tokens(..., recent=False)`` reads live rows only (P17 exempt)."""
    return any(
        keyword.arg == "recent"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in call.keywords
    )


def _keyed_by_recipes(call: ast.Call) -> bool:
    """``…read(..., recipes=…)`` answers for the recipes the target names only."""
    return any(
        keyword.arg == "recipes"
        and not (
            isinstance(keyword.value, ast.Constant) and keyword.value.value is None
        )
        for keyword in call.keywords
    )


def _reader_call(call: ast.Call) -> bool:
    name = _tail(call.func)
    if name == "profiles_naming":
        # Every saved profile's recipes, unless the call names the recipes.
        positional = len(call.args) > 1 and not (
            isinstance(call.args[1], ast.Constant) and call.args[1].value is None
        )
        return not positional and not _keyed_by_recipes(call)
    if name in WHOLE_COLLECTION_READERS:
        return not _non_terminal_only(call)
    return (
        name == "read"
        and isinstance(call.func, ast.Attribute)
        and _tail(call.func.value) in EVIDENCE_CLASSES
        and not _keyed_by_recipes(call)
    )


def _unfiltered_select(
    call: ast.Call, parents: dict[ast.AST, ast.AST], scope: ast.AST
) -> bool:
    """``select(Model)`` of a growing table with nothing narrowing its statement."""
    if not (
        _tail(call.func) == "select"
        and call.args
        and not call.keywords
        # ``select(Job)`` and column selects such as ``select(Job.payload)``.
        and any(
            _tail(argument.value if isinstance(argument, ast.Attribute) else argument)
            in TABLES
            for argument in call.args
        )
    ):
        return False
    return not is_narrowed(call, parents, scope, bool)


def _scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST:
    while node in parents and not isinstance(
        node, ast.FunctionDef | ast.AsyncFunctionDef
    ):
        node = parents[node]
    return node


def scan_source(source: str, *, path: str) -> list[tuple[int, str]]:
    """Lines that read a whole collection outside a collector."""
    if is_collector(path):
        return []
    tree = ast.parse(source)
    exempt = [
        (function.lineno, function.end_lineno or function.lineno)
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        and (
            function.name in COLLECTOR_FUNCTIONS.get(path, frozenset())
            or is_background(function, path)
        )
    ]
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or any(
            start <= node.lineno <= end for start, end in exempt
        ):
            continue
        if _reader_call(node):
            hits.append((node.lineno, "whole-collection reader outside a collector"))
        elif _unfiltered_select(node, parents, _scope(node, parents)):
            hits.append((node.lineno, "select of every row of a growing table"))
    return hits
