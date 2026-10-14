"""Find a fixed wall-clock cap that ends work: the shape P1 forbids.

A wait is not a failure; only a stall of the work's own progress ends it
(``vonk_control.stall_window.progress_verdict``). The forbidden shapes build a
deadline from an instant of the work (or a persisted one from the clock) plus
a constant, and end the work when it passes::

    if now >= row.created_at + timedelta(seconds=LIMIT):   # or now - since > LIMIT
        fail(...)
    operation.recovery_deadline = now + BUDGET              # persisted, never moves

A bound built by ``stall_deadline``/``progress_deadline``/``progress_verdict``
is a stall window and never matches: its anchor moves with progress and with
the owners the work waits on. Freshness checks on evidence (an observation's
``observed_at``, a certificate's ``not_after``) bound evidence, not work, and
are not instants of the work. Leases bound takeover, and a cancel's
observation bound ends observing an ending somebody asked for. An in-process
monotonic wait is a client's own observation bound; the loop rule
(``principle_guards.bounded_loop``) owns it.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path

from .parsed_sources import parse_file

KIND = "fixed-cap-ends-work"

_CLOCK = re.compile(
    r"(?:^|[^\w])(?:now|_now\(|time\.time\(|utcnow\(|_clock\(|clock\(\))"
)
#: An instant of the work itself: accepted, created, started, waiting since.
_INSTANT = re.compile(
    r"(?:^|_)(?:created|started|submitted|accepted|requested|queued)(?:_at)?$"
    r"|_since$|^since$"
)
#: Instants of evidence, not of the work: their windows bound freshness.
_EVIDENCE = re.compile(r"observed|seen|signed|not_after|not_before|expir|updated")
_ENDING_CALL = re.compile(
    r"(?:^|_)(?:fail|failed|failure|cancel|cancelled|supersede|retire|expire|"
    r"exhaust|exhausted|refuse|reject|kill|terminate|withdraw|end)(?:_|$)"
)
_ENDING_WORD = re.compile(r"TIMEOUT|TIMED_OUT|EXHAUSTED|EXPIRED|^FAILED$|^WITHDRAWN$")
#: A persisted deadline of work (a column, a keyword, a document field).
_DEADLINE = re.compile(r"deadline", re.IGNORECASE)
#: Liveness leases and activation fences bound takeover, not the work; a
#: cancel's observation bound ends observing an ending somebody asked for.
_LEASE = re.compile(r"lease|activation|cancel", re.IGNORECASE)


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _unwrap(node: ast.AST) -> ast.AST:
    """``_aware(x)``, ``x.timestamp()``, ``x.isoformat()`` and ``x or now`` read as ``x``."""

    while True:
        if isinstance(node, ast.Call) and len(node.args) == 1 and not node.keywords:
            node = node.args[0]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"timestamp", "astimezone", "replace", "isoformat"}
        ):
            node = node.func.value
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            node = node.values[0]
        else:
            return node


def _clock(node: ast.AST) -> bool:
    return bool(_CLOCK.search(ast.unparse(node)))


class _Scope:
    """The names one function binds to work instants, durations and caps."""

    def __init__(self, function: ast.AST) -> None:
        self.instants: set[str] = set()
        self.durations: set[str] = set()
        self.caps: set[str] = set()
        self.clock_caps: set[str] = set()
        self.elapsed: set[str] = set()
        assigns = [
            (node.targets[0].id, node.value)
            for node in ast.walk(function)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ]
        for _ in range(3):
            for target, value in assigns:
                if self.instant(value):
                    self.instants.add(target)
                if self.duration(value):
                    self.durations.add(target)
                if self.cap(value):
                    self.caps.add(target)
                elif self.cap(value, from_clock=True):
                    self.clock_caps.add(target)
                if self.since(value):
                    self.elapsed.add(target)

    def instant(self, node: ast.AST) -> bool:
        node = _unwrap(node)
        if isinstance(node, ast.IfExp):
            return self.instant(node.body) or self.instant(node.orelse)
        name = _name(node)
        if not name or _clock(node) or _EVIDENCE.search(name):
            return False
        return name in self.instants or bool(_INSTANT.search(name))

    def duration(self, node: ast.AST) -> bool:
        node = _unwrap(node)
        if isinstance(node, ast.Constant):
            return isinstance(node.value, int | float) and not isinstance(
                node.value, bool
            )
        if isinstance(node, ast.Call):
            return _name(node.func) == "timedelta"
        if isinstance(node, ast.BinOp):
            return self.duration(node.left) and self.duration(node.right)
        name = _name(node)
        return name in self.durations or bool(re.fullmatch(r"_?[A-Z][A-Z0-9_]*", name))

    def cap(self, node: ast.AST, *, from_clock: bool = False) -> bool:
        """``instant + constant`` (or ``clock + constant`` when persisted)."""

        node = _unwrap(node)
        if isinstance(node, ast.Name):
            return node.id in self.caps or (from_clock and node.id in self.clock_caps)
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
            return False
        return any(
            self.duration(offset)
            and (self.instant(anchor) or (from_clock and _clock(anchor)))
            for anchor, offset in ((node.left, node.right), (node.right, node.left))
        )

    def since(self, node: ast.AST) -> bool:
        """``clock - instant``: time elapsed since an instant of the work."""

        node = _unwrap(node)
        if isinstance(node, ast.Name):
            return node.id in self.elapsed
        return (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Sub)
            and _clock(node.left)
            and self.instant(node.right)
        )

    def expired_when_true(self, compare: ast.Compare) -> bool | None:
        """Whether the comparison holds once the cap passed (``None``: no cap)."""

        if len(compare.ops) != 1:
            return None
        op = compare.ops[0]
        if not isinstance(op, ast.Lt | ast.LtE | ast.Gt | ast.GtE):
            return None
        left, right = compare.left, compare.comparators[0]
        later = isinstance(op, ast.Gt | ast.GtE)  # the left side is the later one
        if _clock(left) and self.cap(right):
            return later
        if _clock(right) and self.cap(left):
            return not later
        if self.since(left) and self.duration(right):
            return later
        if self.since(right) and self.duration(left):
            return not later
        return None


def _ends_work(statements: list[ast.stmt]) -> bool:
    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, ast.Raise):
                return True
            if isinstance(node, ast.Call) and _ENDING_CALL.search(_name(node.func)):
                return True
            if (
                isinstance(node, ast.keyword)
                and node.arg == "severity"
                and "ERROR" in ast.unparse(node.value).upper()
            ):
                return True
            if (
                isinstance(node, ast.Assign)
                and any(_name(target) == "retryable" for target in node.targets)
                and isinstance(node.value, ast.Constant)
                and node.value.value is False
            ):
                return True
            if (
                isinstance(node, ast.keyword)
                and node.arg in {"failed", "retryable"}
                and isinstance(node.value, ast.Constant)
                and node.value.value is (node.arg == "failed")
            ):
                return True
            if isinstance(node, ast.Attribute | ast.Name) and _ENDING_WORD.search(
                _name(node)
            ):
                return True
    return False


def _expired_branches(
    function: ast.AST, compare: ast.Compare, expired_when_true: bool
) -> Iterator[list[ast.stmt]]:
    """The statements that run once the cap passed."""

    assigned = {
        target.id
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(part is compare for part in ast.walk(node.value))
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    for node in ast.walk(function):
        if not isinstance(node, ast.If | ast.IfExp):
            continue
        direct = any(part is compare for part in ast.walk(node.test))
        named = bool(
            {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)} & assigned
        )
        if not direct and not named:
            continue
        negated = isinstance(node.test, ast.UnaryOp) and isinstance(
            node.test.op, ast.Not
        )
        if expired_when_true != negated:
            yield node.body if isinstance(node, ast.If) else [ast.Expr(node.body)]
        else:
            yield node.orelse if isinstance(node, ast.If) else [ast.Expr(node.orelse)]


def _persisted_deadline(node: ast.AST) -> tuple[str, ast.AST] | None:
    """``(name, value)`` of an attribute or keyword whose name is a deadline."""

    if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and _DEADLINE.search(target.attr)
                and not _LEASE.search(target.attr)
            ):
                return target.attr, node.value
    if (
        isinstance(node, ast.keyword)
        and node.arg is not None
        and _DEADLINE.search(node.arg)
        and not _LEASE.search(node.arg)
    ):
        return node.arg, node.value
    return None


def _deadline_names(function: ast.AST, node: ast.AST) -> set[str]:
    """Deadline names an expression reads, through local aliases."""

    aliases = {
        target.id: value
        for statement in ast.walk(function)
        if isinstance(statement, ast.Assign)
        for target, value in ((t, statement.value) for t in statement.targets)
        if isinstance(target, ast.Name)
    }
    names: set[str] = set()
    pending, seen = [node], set()
    while pending:
        current = pending.pop()
        for part in ast.walk(current):
            name = _name(part)
            if name and _DEADLINE.search(name) and not _LEASE.search(name):
                names.add(name)
            if (
                isinstance(part, ast.Name)
                and part.id in aliases
                and part.id not in seen
            ):
                seen.add(part.id)
                pending.append(aliases[part.id])
    return names


def ending_deadlines(tree: ast.Module) -> set[str]:
    """Deadline names some branch ends work on once the clock passes them."""

    names: set[str] = set()
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                continue
            op = node.ops[0]
            if not isinstance(op, ast.Lt | ast.LtE | ast.Gt | ast.GtE):
                continue
            left, right = node.left, node.comparators[0]
            later = isinstance(op, ast.Gt | ast.GtE)
            if _clock(left) and not _clock(right):
                read, expired = right, later
            elif _clock(right) and not _clock(left):
                read, expired = left, not later
            else:
                continue
            read_names = _deadline_names(function, read)
            if read_names and any(
                _ends_work(branch)
                for branch in _expired_branches(function, node, expired)
            ):
                names |= read_names
    return names


def fixed_cap_lines(
    tree: ast.Module, ended: frozenset[str] | set[str] | None = None
) -> list[tuple[str, int]]:
    """``(function, line)`` of each fixed cap that ends work.

    ``ended`` names the deadlines some branch (anywhere) ends work on; a
    persisted deadline written as a fixed offset is a cap only then.
    """

    ended = ending_deadlines(tree) if ended is None else ended
    found: dict[int, str] = {}
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        scope = _Scope(function)
        producer = bool(_DEADLINE.search(function.name)) and not _LEASE.search(
            function.name
        )
        for node in ast.walk(function):
            if isinstance(node, ast.Compare):
                expired = scope.expired_when_true(node)
                if expired is not None and any(
                    _ends_work(branch)
                    for branch in _expired_branches(function, node, expired)
                ):
                    found.setdefault(node.lineno, function.name)
                continue  # a comparison is never a persisted deadline
            persisted = _persisted_deadline(node)
            if persisted is not None and persisted[0] in ended:
                value: ast.AST | None = persisted[1]
            elif producer and isinstance(node, ast.Return):
                value = node.value
            else:
                continue
            if value is None:
                continue
            candidates = (
                [value.body, value.orelse] if isinstance(value, ast.IfExp) else [value]
            )
            if any(scope.cap(item, from_clock=True) for item in candidates):
                found.setdefault(
                    getattr(node, "lineno", function.lineno), function.name
                )
    return sorted(((name, line) for line, name in found.items()), key=lambda i: i[1])


ROOT = Path(__file__).resolve().parents[2]
#: The trees whose work deadlines the rule covers whole-tree.
ROOTS = ("control/src", "src/cluster_profiles")


def _files() -> list[Path]:
    return [
        path
        for root in ROOTS
        for path in sorted((ROOT / root).rglob("*.py"))
        if "generated_control" not in path.parts and "__pycache__" not in path.parts
    ]


@cache
def tree_ending_deadlines() -> frozenset[str]:
    """Deadline names some branch anywhere in the covered trees ends work on."""
    names: set[str] = set()
    for path in _files():
        names |= ending_deadlines(parse_file(path).tree)
    return frozenset(names)


def tree_sites() -> list[tuple[str, str, int]]:
    """``(path, function, line)`` of every fixed cap in the covered trees."""
    ended = tree_ending_deadlines()
    return [
        (path.relative_to(ROOT).as_posix(), function, line)
        for path in _files()
        for function, line in fixed_cap_lines(parse_file(path).tree, ended)
    ]
