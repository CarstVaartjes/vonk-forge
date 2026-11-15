"""Find a new reader that matches a cached model set by its first requester.

``ModelCacheSet.model_content_sha256`` is the Model digest that first asked for
the set: provenance, not "which Models does this set serve". A release that
re-digests a Model of the same files keeps the old first requester; the new
digest is a verified binding in the manifest. A reader that compares the column
with a Model digest then misses every set a re-digested Model uses (C3: eviction
offered sets a profile needs and miscounted shared Spark files).

Read the set's bindings through ``model_cache.lineage`` (``model_sets``,
``verifies_model``, ``stored_set_bindings``) or, for what an installation runs,
``RecipeInstallation.model_artifact_set_sha256``.

The same holds for what a Spark's installations hold: a query selecting only
``RecipeInstallation.model_content_sha256`` per Spark (joined to
``InstallationNode``) answers "which Models does this Spark hold" by the Model
each installation was made for, and missed every re-digested Model of the same
files (the load review charged them again). Key it by the installation's set.
Queries that read the column next to others (grouping, provenance) are not
this question.
"""

from __future__ import annotations

import ast

KIND = (
    "model set matched by its first-requester Model digest (or a Spark's "
    "installations read by Model digest); read its bindings through "
    "model_cache.lineage or the installation's model_artifact_set_sha256"
)
_PREFIX = "control/src/vonk_control/"
# The owner of the binding rule (first requester plus verified bindings) and
# of the retention unit (first_requested_sets). Only added lines are checked,
# so existing provenance readers elsewhere need no entry.
_ALLOWED = frozenset({_PREFIX + "model_cache/lineage.py"})


def in_scope(path: str) -> bool:
    return path.startswith(_PREFIX) and path.endswith(".py") and path not in _ALLOWED


def _is_column(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "model_content_sha256"
        and isinstance(node.value, ast.Name)
        and node.value.id == "ModelCacheSet"
    )


def _projects_installation_model(call: ast.Call) -> list[int]:
    """The line of a ``select(RecipeInstallation.model_content_sha256)`` under a
    ``.join(InstallationNode, ...)``: the Models a Spark holds, by Model digest."""

    if not (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "join"
        and call.args
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "InstallationNode"
    ):
        return []
    inner: ast.AST = call.func.value
    while isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
        inner = inner.func.value
    if not (
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == "select"
    ):
        return []
    if len(inner.args) != 1:
        return []
    (arg,) = inner.args
    return (
        [arg.lineno]
        if isinstance(arg, ast.Attribute)
        and arg.attr == "model_content_sha256"
        and isinstance(arg.value, ast.Name)
        and arg.value.id == "RecipeInstallation"
        else []
    )


def scan_source(source: str) -> list[int]:
    """Lines comparing the first-requester column with a digest, or reading a
    Spark's installations by Model digest."""

    lines: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            lines.update(_projects_installation_model(node))
        if isinstance(node, ast.Compare) and any(
            _is_column(operand) for operand in (node.left, *node.comparators)
        ):
            if all(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
                lines.add(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"in_", "not_in"}
            and _is_column(node.func.value)
        ):
            lines.add(node.lineno)
    return sorted(lines)
