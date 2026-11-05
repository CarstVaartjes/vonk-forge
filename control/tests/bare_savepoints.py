"""Find a savepoint that is not a ``with`` block.

Fleet events are numbered in the outermost transaction's ``before_commit``.
When ``with sessions.begin()`` exits while a bare ``begin_nested()`` is still
open, SQLAlchemy fires that hook only while the savepoint is open, so every
event the transaction staged is dropped: no burned id, no hole, no refresh.
A ``with session.begin_nested():`` block always ends its savepoint first.
"""

from __future__ import annotations

import ast

KIND = "begin_nested() outside a with block; an open savepoint drops Fleet events"


def scan_source(source: str) -> list[int]:
    tree = ast.parse(source)
    managed = {
        id(item.context_expr)
        for node in ast.walk(tree)
        if isinstance(node, ast.With | ast.AsyncWith)
        for item in node.items
    }
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "begin_nested"
        and id(node) not in managed
    )
