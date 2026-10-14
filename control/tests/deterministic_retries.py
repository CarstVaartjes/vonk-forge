"""Find a broad ``except`` that retries without asking whether the cause is transient.

A deterministic error (the same input raises the same error on every attempt)
must end its operation with evidence; only a transient cause is retried.  A
handler that catches a broad builtin family and then schedules a retry, defers,
holds, replans or wraps the error as "retry later" must consult the one
classifier (``disposition``, or a helper built on it) or pass the error's
``definite`` verdict on.  Count-free: the caller reports only added lines.

:func:`scan_dispositions` is the whole-tree half (``test_failure_disposition``):
no failure is reported, counted or limited outside the one disposition.
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
CLASSIFIERS = frozenset(
    {
        "availability_disposition",
        "disposition",
        "is_definite",
        "result_disposition",
        "retry_later_unless_deterministic",
    }
)
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


# ---------------------------------------------------------------------------
# One failure disposition (whole-tree).  The verdict of a failure is
# ``failure_classification.disposition``; a failed lifecycle report is built by
# ``Reported.failed`` (which derives ``retryable``/``consumes_retry``); the one
# failure limit and retry clock live in ``recovery_policy`` and the lifecycle
# core.  Nothing else hand-sets a retry flag, keeps a private limit, counts its
# own attempts, or lets a busy/not-ready type decide whether a retry is spent.

#: The owners of the primitive itself.
DISPOSITION_OWNERS = frozenset(
    {
        "control/src/vonk_control/failure_classification.py",
        "control/src/vonk_control/lifecycle/core.py",
        "control/src/vonk_control/lifecycle/types.py",
        "control/src/vonk_control/recovery_policy.py",
    }
)
_RETRY_FLAGS = frozenset({"retryable", "consumes_retry"})
_NON_FAILURE_OUTCOMES = frozenset(
    {"Outcome.DONE", "Outcome.CANCELLED", "Outcome.UNKNOWN"}
)
#: An owner's own persisted attempt counter, and the core's retry count.
_OWN_COUNTER = re.compile(r"._attempts$")
_COUNTER = re.compile(r"._attempts$|^retry_count$")
_LIMIT = re.compile(r"MAX|max_")
_WAIT_TYPE = re.compile(r"(Busy|NotReady)$")
_COUNTING_KEYWORDS = frozenset({"consume_retry", "consumes_retry", "ongoing_intent"})


def _counter(node: ast.AST, pattern: re.Pattern[str] = _COUNTER) -> bool:
    return any(
        isinstance(part, (ast.Name, ast.Attribute))
        and bool(pattern.search(_name(part)))
        for part in ast.walk(node)
    )


def _counts(function: ast.AST) -> bool:
    """Whether a function decides whether a retry is spent."""

    for node in ast.walk(function):
        if isinstance(node, ast.keyword) and node.arg in _COUNTING_KEYWORDS:
            return True
        if isinstance(node, (ast.AugAssign, ast.BinOp)) and _counter(
            node, _OWN_COUNTER
        ):
            return True
    return False


def scan_dispositions(source: str, *, path: str) -> list[tuple[int, str]]:
    """Sites that decide a failure's retry outside the one disposition."""

    if path in DISPOSITION_OWNERS:
        return []
    tree = ast.parse(source)
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called = _name(node.func)
            flags = {keyword.arg for keyword in node.keywords} & _RETRY_FLAGS
            if called == "Reported" and (
                flags
                or not node.args
                or ast.unparse(node.args[0]) not in _NON_FAILURE_OUTCOMES
            ):
                sites.append((node.lineno, "failure reported outside Reported.failed"))
            elif called == "replace" and flags:
                sites.append((node.lineno, "retry flag set by hand"))
            elif called == "RecoveryPolicy":
                sites.append((node.lineno, "private retry limit"))
        elif isinstance(node, ast.Attribute) and node.attr == "max_failures":
            sites.append((node.lineno, "private retry limit"))
        elif (
            isinstance(node, (ast.AugAssign, ast.BinOp))
            and isinstance(node.op, ast.Add)
            and _counter(
                node.target if isinstance(node, ast.AugAssign) else node, _OWN_COUNTER
            )
        ):
            sites.append((node.lineno, "retry counted by hand"))
        elif isinstance(node, ast.Compare):
            operands = (node.left, *node.comparators)
            if any(_counter(item) for item in operands) and any(
                isinstance(part, (ast.Name, ast.Attribute))
                and _LIMIT.search(_name(part))
                for item in operands
                for part in ast.walk(item)
            ):
                sites.append((node.lineno, "retry counted by hand"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _counts(
            node
        ):
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and _name(call.func) == "isinstance"
                    and len(call.args) == 2
                ):
                    kinds = (
                        call.args[1].elts
                        if isinstance(call.args[1], ast.Tuple)
                        else [call.args[1]]
                    )
                    if any(_WAIT_TYPE.search(_name(kind)) for kind in kinds):
                        sites.append(
                            (call.lineno, "error type decides whether a retry is spent")
                        )
    return sorted(set(sites))
