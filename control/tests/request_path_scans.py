"""Detect unbounded catalog/history materialization in request readers.

A limit bounds a page; an identity predicate scopes a detail or its related
facts. State/kind filters alone bound neither history nor the catalog. This
syntax guard complements SQL capture and producer/consumer behavior tests.
"""

from __future__ import annotations

import ast
from collections.abc import Callable

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


# The only calls that narrow a select: chained on it, never the builtin filter().
NARROWING_METHODS = frozenset({"where", "filter", "filter_by", "having"})


def is_narrowing(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (
        is_limit(node)
        or (
            isinstance(node.func, ast.Attribute) and node.func.attr in NARROWING_METHODS
        )
    )


def _chain(
    expression: ast.AST, parents: dict[ast.AST, ast.AST]
) -> tuple[list[ast.Call], ast.AST]:
    """Narrowing calls chained directly on ``expression``, and the chain's top."""
    found: list[ast.Call] = []
    current = expression
    while True:
        attribute = parents.get(current)
        outer = parents.get(attribute) if attribute is not None else None
        if not (
            isinstance(attribute, ast.Attribute)
            and attribute.value is current
            and isinstance(outer, ast.Call)
            and outer.func is attribute
        ):
            return found, current
        # EXISTS answers a boolean in the database; it reads no rows back.
        if is_narrowing(outer) or _tail(outer.func) == "exists":
            found.append(outer)
        current = outer


def _equalities(items: list[ast.expr], subquery: ast.AST) -> list[ast.expr]:
    """``==`` conditions next to the subquery, positional or inside ``and_``/``&``."""
    found: list[ast.expr] = []
    for item in items:
        if isinstance(item, ast.Call) and _tail(item.func) == "and_":
            found.extend(_equalities([*item.args], subquery))
        elif isinstance(item, ast.BinOp) and isinstance(item.op, ast.BitAnd):
            found.extend(_equalities([item.left, item.right], subquery))
        elif (
            isinstance(item, ast.Compare)
            and all(isinstance(op, ast.Eq) for op in item.ops)
            and not any(part is subquery for part in ast.walk(item))
        ):
            found.append(item)
    return found


def _semi_join(subquery: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.Call]:
    """A subquery in a where/filter/having that also has an ``==`` condition.

    The target-keyed semi-join: ``where(slug == d, digest.in_(select(...)))``.
    The subquery must reach that call through calls, comparisons and ``and_``/
    ``&`` only; ``or_``, ``|``, ``+`` and every other operator give no credit.
    """
    current = subquery
    while True:
        parent = parents.get(current)
        if parent is None or isinstance(parent, ast.stmt):
            return []
        if (
            isinstance(parent, ast.Call)
            and current is not parent.func
            and is_narrowing(parent)
        ):
            keyed = _equalities(
                [*parent.args, *(word.value for word in parent.keywords)], subquery
            )
            return (
                [ast.Call(func=parent.func, args=keyed, keywords=[])] if keyed else []
            )
        if not (
            isinstance(parent, ast.keyword | ast.Compare | ast.Attribute)
            or (isinstance(parent, ast.Call) and _tail(parent.func) != "or_")
            or (isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.BitAnd))
        ):
            return []
        current = parent


def _rebinds(statement: ast.AST, name: str, parents: dict[ast.AST, ast.AST]) -> bool:
    """``q = q.where(...)``, or an ``if`` that does it in both branches."""
    if isinstance(statement, ast.Assign):
        return (
            [t.id for t in statement.targets if isinstance(t, ast.Name)] == [name]
            and len(statement.targets) == 1
            and any(
                isinstance(load, ast.Name)
                and load.id == name
                and _chain(load, parents)[0]
                for load in ast.walk(statement.value)
            )
        )
    if isinstance(statement, ast.If):
        return any(_rebinds(item, name, parents) for item in statement.body) and any(
            _rebinds(item, name, parents) for item in statement.orelse
        )
    return False


def _rebound_after(
    assignment: ast.Assign, name: str, parents: dict[ast.AST, ast.AST]
) -> list[ast.stmt]:
    """The statements after a rebind in the assignment's own block.

    Only loads lexically inside them are narrowed on every path; a rebind in
    another block, or a load outside these statements, gets no credit.
    """
    owner = parents.get(assignment)
    for field in ("body", "orelse", "finalbody"):
        block = getattr(owner, field, None)
        if isinstance(block, list) and assignment in block:
            for index in range(block.index(assignment) + 1, len(block)):
                if _rebinds(block[index], name, parents):
                    return block[index + 1 :]
    return []


def is_narrowed(
    call: ast.Call,
    parents: dict[ast.AST, ast.AST],
    scope: ast.AST,
    judge: Callable[[list[ast.AST]], bool],
) -> bool:
    """Fail closed: is ``call`` narrowed by something ``judge`` accepts?

    Credit comes only from a narrowing call chained directly on the select, the
    keyed semi-join above, or, for ``q = select(...)``, every load of ``q``
    being narrowed that way (a later ``q = q.where(...)`` in the same block
    narrows the loads after it). Nothing beside it counts: sibling arguments, set
    operations, other operators, outer statements.
    """

    def narrowed(expression: ast.AST) -> bool:
        chained, top = _chain(expression, parents)
        nodes: list[ast.AST] = [*chained, *_semi_join(top, parents)]
        return judge(nodes)

    if narrowed(call):
        return True
    assignment = parents.get(_chain(call, parents)[1])
    if not (
        isinstance(assignment, ast.Assign)
        and len(assignment.targets) == 1
        and isinstance(assignment.targets[0], ast.Name)
    ):
        return False
    name = assignment.targets[0].id
    loads = [
        part
        for part in ast.walk(scope)
        if isinstance(part, ast.Name)
        and part.id == name
        and isinstance(part.ctx, ast.Load)
    ]
    covered = {
        id(part)
        for tail in _rebound_after(assignment, name, parents)
        for part in ast.walk(tail)
    }
    return bool(loads) and all(narrowed(load) or id(load) in covered for load in loads)


def _key(node: ast.AST) -> bool:
    name = _tail(node)
    return name in {"id", "slug", "publisher", "content_digest"} or name.endswith(
        ("_id", "_sha256")
    )


def _scoped(nodes: list[ast.AST]) -> bool:
    # EXISTS credits only the select it is chained on, never one inside a where.
    if any(
        isinstance(node, ast.Call) and _tail(node.func) == "exists" for node in nodes
    ):
        return True
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
            if not is_narrowed(call, parents, function, _scoped):
                sites.append(call.lineno)
    return sorted(set(sites))
