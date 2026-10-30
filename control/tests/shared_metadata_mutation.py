"""Find a test that retypes a column of the shared ORM metadata.

``Base.metadata.tables[t].c[c].type = BigInteger()`` (or the same through a
model's ``__table__``) changes what every later test on the worker binds:
SQLAlchemy memoizes the swapped bind type on the ORM attribute at its first
comparison, so restoring the column afterwards leaves a stale BIGINT and an
unrelated test fails with ``text <> bigint`` (flaky CI, #1968). Prove a
historical column type on a private ``MetaData`` or a private engine dialect
(``dialect.colspecs``) instead.
"""

from __future__ import annotations

import ast
import re

KIND = "shared-metadata-column-type-mutation"
_SHARED = re.compile(r"\bBase\.metadata\b|\.__table__\b|\.__mapper__\b")


def _shared_type(target: ast.expr) -> bool:
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "type"
        and _SHARED.search(ast.unparse(target.value)) is not None
    )


def _shared_setattr(node: ast.Call) -> bool:
    if ast.unparse(node.func).split(".")[-1] != "setattr":
        return False
    strings = [
        argument.value
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    ]
    return "type" in strings and any(
        _SHARED.search(ast.unparse(argument)) for argument in node.args[:1]
    )


def scan_source(source: str) -> list[int]:
    """Lines that assign a column ``type`` reached through shared metadata."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            hit = any(_shared_type(target) for target in node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            hit = _shared_type(node.target)
        elif isinstance(node, ast.Call):
            hit = _shared_setattr(node)
        else:
            continue
        if hit:
            lines.append(node.lineno)
    return lines
