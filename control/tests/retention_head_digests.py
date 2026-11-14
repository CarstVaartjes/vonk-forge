"""Find a retention reader that decides "is this the head" by content digest.

A release that changes only metadata gives a recipe revision a new content
digest and the same execution key. Retention that compares a revision's
``content_digest`` with the head's then drops the loaded profile's idle
installation 24 hours after every such release. "Is this installation what the
head runs" is the head revision itself or ``content_identity.runs_identically``
(see ``unused_storage_collection.references._head_runs_it``).
"""

from __future__ import annotations

import ast

KIND = (
    "retention compares a content digest with the head's; "
    "use the head revision or content_identity.runs_identically"
)
_SCOPE = "control/src/vonk_control/unused_storage_collection/"
_FLEET_REMOVAL = "control/src/vonk_control/model_cache/fleet_removal.py"
_HEAD_WORDS = ("head", "newest")


def in_scope(path: str) -> bool:
    return (path.startswith(_SCOPE) and path.endswith(".py")) or path == _FLEET_REMOVAL


def _names(node: ast.AST) -> set[str]:
    return {
        child.attr if isinstance(child, ast.Attribute) else child.id
        for child in ast.walk(node)
        if isinstance(child, (ast.Attribute, ast.Name))
    }


def _names_head(node: ast.AST) -> bool:
    return any(word in name.casefold() for name in _names(node) for word in _HEAD_WORDS)


def scan_source(source: str) -> list[int]:
    """Lines comparing a ``content_digest`` with something named head or newest."""

    lines: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare) or not all(
            isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in node.ops
        ):
            continue
        operands = [node.left, *node.comparators]
        if any(
            "content_digest" in _names(digest) and _names_head(head)
            for i, digest in enumerate(operands)
            for j, head in enumerate(operands)
            if i != j
        ):
            lines.add(node.lineno)
    return sorted(lines)
