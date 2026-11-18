"""Find a Controller revocation write whose commit may stay asynchronous.

Controller connections commit with ``synchronous_commit=off``. A write that
revokes access or changes a credential, and is acknowledged to the caller, must
call ``require_durable_commit`` in the same function. Enrollment writes get it
from ``EnrollmentCore._transaction``.
"""

from __future__ import annotations

import ast

KIND = (
    "revocation or credential write without require_durable_commit; Controller "
    "commits are asynchronous, so a crash could undo an acknowledged revocation"
)
FIELDS = frozenset({"revoked_at", "ca_revoked_at", "disabled_at", "password_verifier"})
ENROLLMENT = "control/src/vonk_control/enrollment/"


def in_scope(path: str) -> bool:
    return (
        path.startswith("control/src/vonk_control/")
        and path.endswith(".py")
        and not path.startswith(ENROLLMENT)
    )


def _calls_helper(scope: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Name)
                and node.func.id == "require_durable_commit"
            )
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "require_durable_commit"
            )
        )
        for node in ast.walk(scope)
    )


def _writes(scope: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(scope):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        lines.extend(
            target.lineno
            for target in targets
            if isinstance(target, ast.Attribute) and target.attr in FIELDS
        )
        if isinstance(node, ast.Call):
            lines.extend(node.lineno for kw in node.keywords if kw.arg in FIELDS)
    return lines


def scan_source(source: str) -> list[int]:
    tree = ast.parse(source)
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    inner = {id(n) for f in functions for n in ast.walk(f) if n is not f}
    hits: set[int] = set()
    for function in functions:
        # A nested function is judged by the outermost enclosing function.
        if id(function) not in inner and not _calls_helper(function):
            hits.update(_writes(function))
    return sorted(hits)
