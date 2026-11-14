"""Find a new reader that matches a cached model set by its first requester.

``ModelCacheSet.model_content_sha256`` is the Model digest that first asked for
the set: provenance, not "which Models does this set serve". A release that
re-digests a Model of the same files keeps the old first requester; the new
digest is a verified binding in the manifest. A reader that compares the column
with a Model digest then misses every set a re-digested Model uses (C3: eviction
offered sets a profile needs and miscounted shared Spark files).

Read the set's bindings through ``model_cache.lineage`` (``model_sets``,
``verifies_model``, ``stored_set_bindings``) or, for what an installation runs,
``RecipeInstallation.model_artifact_set_sha256``. Each allowlisted file below is
a classified existing reader that wants provenance on purpose.
"""

from __future__ import annotations

import ast

KIND = (
    "model set matched by its first-requester Model digest; read its bindings "
    "through model_cache.lineage or the installation's model_artifact_set_sha256"
)
_PREFIX = "control/src/vonk_control/"
_ALLOWED = frozenset(
    _PREFIX + name
    for name in (
        # The owner of the binding rule (first requester plus verified bindings)
        # and of the retention unit (first_requested_sets).
        "model_cache/lineage.py",
        # Resolution reads provenance by design.
        "model_cache/resolution.py",
        # The retention unit: the sets first downloaded under this digest; the
        # profile check beside it reads the bindings (verifies_model).
        "unused_storage_collection/references.py",
        # Live operations' manifests; the Model definitions walk after it
        # covers every file by content.
        "spark_store_retention.py",
        # Library: a non-head revision with cached bytes stays listable
        # (provenance), and local state ORs in verifies_model.
        "library_projection/catalog.py",
        "library_projection/local_state.py",
    )
)


def in_scope(path: str) -> bool:
    return path.startswith(_PREFIX) and path.endswith(".py") and path not in _ALLOWED


def _is_column(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "model_content_sha256"
        and isinstance(node.value, ast.Name)
        and node.value.id == "ModelCacheSet"
    )


def scan_source(source: str) -> list[int]:
    """Lines comparing the first-requester column with a digest."""

    lines: set[int] = set()
    for node in ast.walk(ast.parse(source)):
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
