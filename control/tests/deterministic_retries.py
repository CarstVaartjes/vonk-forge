"""Find a broad ``except`` that retries without asking whether the cause is transient.

A deterministic error (the same input raises the same error on every attempt)
must end its operation with evidence; only a transient cause is retried.  A
handler that catches a broad builtin family and then schedules a retry, defers,
holds, replans or wraps the error as "retry later" or an unknown outcome must
consult the one classifier (``disposition``, or a helper built on it) or pass the error's
``definite`` verdict on.  Count-free: the caller reports only added lines.

:func:`scan_dispositions` is the whole-tree half (``test_failure_disposition``):
no failure is reported, counted or limited outside the one disposition.
"""

from __future__ import annotations

import ast
import re

from vonk_agent_protocol import TERMINAL_LIFECYCLE_STATES, LifecycleState

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
_RETRY_RAISE = re.compile(
    r"RetryLater$|^UnsettledOutcome$|^BookkeepingUnknown$|^UnknownOutcomeError$"
)


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
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_schedules(target) for target in targets):
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


# A retry scheduled by writing its due time (``self._retry_at[task] = ...``,
# ``row.next_action_at = ...``) instead of calling a retry function.
_SCHEDULE_TARGET = re.compile(r"retry_at$|^next_\w*_at$")


def _schedules(target: ast.AST) -> bool:
    if isinstance(target, ast.Subscript):
        target = target.value
    return isinstance(target, (ast.Name, ast.Attribute)) and bool(
        _SCHEDULE_TARGET.search(_name(target))
    )


def scan_repeats(source: str) -> list[int]:
    """Lines of handlers that schedule a retry without showing its repetition.

    The same failure retried on every tick is a platform fault an operator must
    see: the handler records it with ``note_repeat`` (surface only; it never
    ends, blocks or caps the retry).  Count-free: the caller reports only
    added lines.
    """

    sites: list[int] = []
    for handler in ast.walk(ast.parse(source)):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        body = list(ast.walk(ast.Module(body=handler.body, type_ignores=[])))
        if any(
            isinstance(node, ast.Call) and _name(node.func) == "note_repeat"
            for node in body
        ):
            continue
        sites.extend(
            node.lineno
            for node in body
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
            and any(
                _schedules(target)
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            )
        )
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


#: The agent's one classifier: the status table and the failure-kind mapping.
RUST_DISPOSITION_OWNERS = frozenset(
    {
        "rust/crates/vonk-agent/src/client/errors.rs",
        "rust/crates/vonk-agent/src/executor/failures.rs",
    }
)
_RUST_STATUS_LITERAL = re.compile(
    r"status[^;\n]{0,40}?(?:==|!=|,|=>|\()\s*(?:Some\()?\s*[45]\d\d\b"
    r"|matches!\([^,\n]*status[^,\n]*,\s*(?:Some\()?[45]\d\d"
)


def scan_rust_dispositions(
    source: str, *, path: str, test_lines: frozenset[int] | set[int] = frozenset()
) -> list[tuple[int, str]]:
    """Agent lines that read an HTTP error status by hand instead of
    ``ClientError::disposition`` (the shared contract's table)."""

    if path in RUST_DISPOSITION_OWNERS:
        return []
    return [
        (number, "HTTP status classified outside the one disposition")
        for number, line in enumerate(source.splitlines(), start=1)
        if number not in test_lines
        and _RUST_STATUS_LITERAL.search(line.split("//", 1)[0])
    ]


# A standing owner bounds its retry rate; only a request-local retry may spend
# a finite count. This is intentionally syntax detection, not a liveness proof.
_REPETITION = re.compile(r"attempt|retr(?:y|ies)|repeat|failure", re.IGNORECASE)
_TERMINAL = frozenset(
    state.name for state in (*TERMINAL_LIFECYCLE_STATES, LifecycleState.NEEDS_OPERATOR)
)


def _ends_work(node: ast.AST) -> bool:
    for part in ast.walk(node):
        if isinstance(part, ast.Return) and (
            part.value is None
            or isinstance(part.value, ast.Constant)
            and part.value.value is None
        ):
            return True
        if isinstance(part, ast.Attribute) and part.attr in _TERMINAL:
            return True
        if (
            isinstance(part, ast.keyword)
            and part.arg in {"next_action_at", "next_at"}
            and isinstance(part.value, ast.Constant)
            and part.value.value is None
        ):
            return True
        if isinstance(part, (ast.Assign, ast.AnnAssign)):
            targets = part.targets if isinstance(part, ast.Assign) else [part.target]
            if (
                any(
                    _name(target) in {"next_action_at", "next_at"} for target in targets
                )
                and isinstance(part.value, ast.Constant)
                and part.value.value is None
            ):
                return True
        if isinstance(part, ast.Call) and _name(part.func) in {
            "fail",
            "_fail",
            "finish",
            "_end",
            "make_blocker",
        }:
            return True
    return False


def _request_or_cancel(test: ast.AST) -> bool:
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(_request_or_cancel(value) for value in test.values)
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "cancel_requested"
        or isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Name)
        and test.operand.id == "ongoing_intent"
    )


def scan_count_endings(source: str) -> list[int]:
    """Attempt/repetition comparisons that terminate work or remove its action.

    Allow explicit cancellation and the shared policy's request-local branch.
    A bounded local loop may end its request; it cannot decide the lifetime
    of a durable row.
    """
    tree = ast.parse(source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    sites: list[int] = []
    for branch in ast.walk(tree):
        if not isinstance(branch, ast.If):
            continue
        ancestors: list[ast.AST] = []
        conditions = [branch.test]
        cursor: ast.AST = branch
        while cursor in parents:
            parent = parents[cursor]
            if isinstance(parent, ast.If) and cursor in parent.body:
                conditions.append(parent.test)
            ancestors.append(parent)
            cursor = parent
        # These are explicit endings of cancellation or a request-local budget,
        # never continuing authorized work.
        if any(_request_or_cancel(condition) for condition in conditions):
            continue
        if any(
            isinstance(loop, ast.For)
            and isinstance(loop.target, ast.Name)
            and isinstance(loop.iter, ast.Call)
            and _name(loop.iter.func) == "range"
            and any(
                isinstance(part, ast.Name) and part.id == loop.target.id
                for part in ast.walk(branch.test)
            )
            for loop in ancestors
        ) and not any(
            isinstance(part, ast.Attribute)
            and part.attr in {"state", "next_at", "next_action_at", *_TERMINAL}
            for statement in branch.body
            for part in ast.walk(statement)
        ):
            continue
        function = next(
            (
                node
                for node in ancestors
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ),
            tree,
        )
        counters = {
            _name(part)
            for part in ast.walk(function)
            if isinstance(part, (ast.Name, ast.Attribute))
            and _REPETITION.search(_name(part))
        }
        for assignment in ast.walk(function):
            if isinstance(assignment, ast.Assign) and _counter(
                assignment.value, _REPETITION
            ):
                counters.update(_name(target) for target in assignment.targets)
        comparisons = [
            part for part in ast.walk(branch.test) if isinstance(part, ast.Compare)
        ]
        limited = any(
            any(_name(part) in counters for part in ast.walk(comparison))
            and any(
                isinstance(op, (ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq))
                for op in comparison.ops
            )
            for comparison in comparisons
        ) or any(
            isinstance(part, ast.Call) and _name(part.func) == "exhausted"
            for part in ast.walk(branch.test)
        )
        if limited and _ends_work(ast.Module(body=branch.body, type_ignores=[])):
            sites.extend(
                [
                    branch.lineno,
                    *(
                        part.lineno
                        for statement in branch.body
                        for part in ast.walk(statement)
                        if isinstance(
                            part, (ast.Return, ast.Call, ast.Assign, ast.AnnAssign)
                        )
                    ),
                ]
            )
    return sorted(set(sites))


def scan_recovery_policy_users(source: str, *, path: str) -> list[int]:
    """All durable policy callers must opt into continuing intent explicitly.

    The policy itself owns request-local exhaustion; its callers are standing
    owners or checkpoint owners that reconcile effects before another attempt.
    """
    if path == "control/src/vonk_control/recovery_policy.py":
        return []
    return [
        call.lineno
        for call in ast.walk(ast.parse(source))
        if isinstance(call, ast.Call)
        and _name(call.func) == "next_attempt"
        and not any(
            keyword.arg == "ongoing_intent"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in call.keywords
        )
    ]
