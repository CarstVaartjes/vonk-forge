"""A Job's owner is read from ``jobs.owner_kind`` / ``jobs.owner_id`` in SQL (P10).

The database generates both columns from the payload and indexes them
(``ix_jobs_owner``). A query that extracts ``Job.payload["owner_id"]`` is a JSON
extraction per row and a scan; an added Controller line may not do it.
"""

from __future__ import annotations

import ast

KIND = (
    "Job owner read from the payload in SQL; use Job.owner_kind / Job.owner_id "
    "(indexed, generated from the payload)"
)
SCOPE = "control/src/vonk_control/"
_OWNER_KEYS = frozenset({"owner_kind", "owner_id"})


def in_scope(path: str) -> bool:
    return path.startswith(SCOPE) and path.endswith(".py")


def _job_payload(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "payload"
        and isinstance(node.value, ast.Name)
        and node.value.id == "Job"
    )


def scan_source(source: str) -> list[tuple[int, str]]:
    """Lines that subscript ``Job.payload`` with an owner key."""

    return [
        (node.lineno, KIND)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Subscript)
        and _job_payload(node.value)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value in _OWNER_KEYS
    ]
