"""Find a broad ``except`` that retries without asking whether the cause is transient.

A deterministic error (the same input raises the same error on every attempt)
must end its operation with evidence; only a transient cause is retried.  A
handler that catches a broad builtin family and then schedules a retry, defers,
holds, replans or wraps the error as "retry later" must consult the one
classifier (``is_transient``, or a helper built on it) or pass the error's
``definite`` verdict on.  Count-free: the caller reports only added lines.
"""

from __future__ import annotations

import ast
import re

BROAD = frozenset(
    {
        "AttributeError",
        "BaseException",
        "Exception",
        "KeyError",
        "LookupError",
        "RuntimeError",
        "TypeError",
        "ValidationError",
        "ValueError",
    }
)
# Names that decide a retry from the error itself.
CLASSIFIERS = frozenset({"is_transient", "retry_later_unless_deterministic"})
_RETRY_CALL = re.compile(r"retry|^_?hold_|^_?defer|requeue|reschedule")
_RETRY_RAISE = re.compile(r"RetryLater$|^UnsettledOutcome$|^BookkeepingUnknown$")


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _caught(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {"BaseException"}
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {_name(item) for item in types}


def _retry_lines(handler: ast.ExceptHandler) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Raise) and node.exc is not None:
            raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if _RETRY_RAISE.search(_name(raised)):
                lines.append(node.lineno)
        elif isinstance(node, ast.Call):
            if _RETRY_CALL.search(_name(node.func)) or any(
                keyword.arg == "replan"
                and not (
                    isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                )
                for keyword in node.keywords
            ):
                lines.append(node.lineno)
    return lines


def _classified(handler: ast.ExceptHandler) -> bool:
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, (ast.Name, ast.Attribute)) and _name(node) in CLASSIFIERS:
            return True
        if isinstance(node, ast.keyword) and node.arg == "definite":
            return True
    return False


def scan_source(source: str) -> list[int]:
    """Lines of broad handlers that retry a possibly deterministic error."""

    sites: list[int] = []
    for handler in ast.walk(ast.parse(source)):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        if not _caught(handler) & BROAD or _classified(handler):
            continue
        retries = _retry_lines(handler)
        if retries:
            sites.extend([handler.lineno, *retries])
    return sites
