"""Detect unbounded catalog/history materialization in request readers.

A limit bounds a page; an identity predicate scopes a detail or its related
facts. State/kind filters alone bound neither history nor the catalog. This
syntax guard complements SQL capture and producer/consumer behavior tests.
"""

from __future__ import annotations

import ast

TABLES = frozenset(
    {
        "CatalogDocumentRevision",
        "CatalogDocument",
        "FleetProfile",
        "FleetProfileApplication",
        "InstallationNode",
        "Job",
        "JobAttempt",
        "AgentOperation",
        "ModelCacheOperation",
        "ModelCacheSet",
        "ModelCacheSetArtifact",
        "NodeInventorySnapshot",
        "NodeTelemetrySample",
        "RecipeBuild",
        "RecipeInstallation",
        "RecipeRun",
        "RunNode",
    }
)


def _tail(node: ast.AST) -> str:
    return (
        node.attr
        if isinstance(node, ast.Attribute)
        else node.id
        if isinstance(node, ast.Name)
        else ""
    )


# Index construction is an explicit background/startup boundary: the one list
# both select rules (here and in ``whole_collection_readers``) exempt.
BACKGROUND_FUNCTIONS = frozenset(
    {("control/src/vonk_control/library_projection/catalog.py", "refresh_catalog")}
)


def is_background(function: ast.AST, path: str) -> bool:
    return (
        isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        and (path, function.name) in BACKGROUND_FUNCTIONS
    )


def is_limit(node: ast.AST) -> bool:
    """``.limit(n)`` with a real bound; ``limit(None)`` is unbounded."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "limit"
        and bool(node.args)
        and not (isinstance(node.args[0], ast.Constant) and node.args[0].value is None)
    )


def statement_nodes(
    call: ast.Call, parents: dict[ast.AST, ast.AST], scope: ast.AST
) -> list[ast.AST]:
    """The outermost statement holding ``call`` and later uses of its variable.

    A select inside an IN-subquery is judged with the query around it.
    """
    query: ast.AST = call
    while query in parents and isinstance(parents[query], ast.Call | ast.Attribute):
        query = parents[query]
    nodes = [query]
    assignment = parents.get(query)
    if isinstance(assignment, ast.Assign):
        names = {_tail(target) for target in assignment.targets}
        nodes.extend(
            part
            for part in ast.walk(scope)
            if isinstance(part, ast.Call)
            and isinstance(part.func, ast.Attribute)
            and _tail(part.func.value) in names
        )
    return nodes


def _key(node: ast.AST) -> bool:
    name = _tail(node)
    return name in {"id", "slug", "publisher", "content_digest"} or name.endswith(
        ("_id", "_sha256")
    )


def _scoped(nodes: list[ast.AST]) -> bool:
    for root in nodes:
        for node in ast.walk(root):
            if (
                isinstance(node, ast.Call)
                and _tail(node.func) == "recipe_content_membership"
            ):
                return True
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if is_limit(node):
                    return True
                if node.func.attr in {"in_"} and _key(node.func.value):
                    return True
            if (
                isinstance(node, ast.Compare)
                and _key(node.left)
                and all(isinstance(op, ast.Eq) for op in node.ops)
                and not all(
                    isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id in TABLES
                    for value in node.comparators
                )
            ):
                return True
    return False


def scan_tree(tree: ast.Module, *, path: str) -> list[int]:
    if not (
        path.endswith("_api.py") or "/api/" in path or "/library_projection/" in path
    ):
        return []
    sites: list[int] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if is_background(function, path):
            continue
        parents = {
            child: parent
            for parent in ast.walk(function)
            for child in ast.iter_child_nodes(parent)
        }
        for call in ast.walk(function):
            if isinstance(call, ast.Call) and _tail(call.func) in {
                "_model_storage",
                "model_storage",
                "inventory",
                "reconcile_storage",
                "_stored_object",
                "lstat",
                "stat",
                "storage_summary",
                "iterdir",
                "scandir",
                "walk",
                "disk_usage",
            }:
                sites.append(call.lineno)
            if (
                isinstance(call, ast.Call)
                and _tail(call.func) == "_catalog_documents"
                and not any(keyword.arg == "digests" for keyword in call.keywords)
            ):
                sites.append(call.lineno)
            if not isinstance(call, ast.Call) or _tail(call.func) != "select":
                continue
            if not any(
                isinstance(part, ast.Name) and part.id in TABLES
                for argument in call.args
                for part in ast.walk(argument)
            ):
                continue
            nodes = statement_nodes(call, parents, function)
            if not _scoped(nodes):
                sites.append(call.lineno)
    return sorted(set(sites))
