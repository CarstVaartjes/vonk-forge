"""Find synchronous I/O run directly inside an ``async def``.

The API is one uvicorn process, so a session or repository call, a file open,
an fsync, a flock, ``time.sleep`` or an ``httpx2.Client`` call made on the
event loop freezes every other request until it returns. Run the sync segment
with ``await asyncio.to_thread(fn, *args)`` (or ``run_in_threadpool``), or make
the handler a plain ``def`` so FastAPI runs it in its threadpool.

A site that must stay on the loop says why on its own line (or the line above)
with ``# sequential: <reason>``, the reason one of ``REASONS``. Lexical: I/O
reached through a helper that is not named here is not seen, and the bodies of
nested ``def``/``lambda`` are skipped because they run where they are called.
"""

from __future__ import annotations

import ast
import re

KIND = (
    "sync I/O inside async def; use await asyncio.to_thread(...) or add "
    "'# sequential: data-dependency|ordering|one-session|one-resource|not-hot'"
)
BAD_REASON_KIND = (
    "'# sequential:' needs a reason from data-dependency, ordering, "
    "one-session, one-resource, not-hot"
)
REASONS = frozenset(
    {"data-dependency", "ordering", "one-session", "one-resource", "not-hot"}
)
MARKER = re.compile(r"#\s*sequential:\s*([\w-]*)")
# Receivers (the last attribute or name before the method) that are a database
# session factory, a session or a repository; a parameter annotated
# ``...Repository`` (and the attribute it is stored in) is one too.
_RECEIVER = re.compile(
    r"(?:^|_)(?:sessions?|session_factory|repos?|repositor(?:y|ies))$"
)
_BARE_CALLS = frozenset({"open"})
# Dotted calls that block: (module or object, attribute).
_DOTTED_CALLS = frozenset(
    {
        ("time", "sleep"),
        ("os", "fsync"),
        ("os", "fdatasync"),
        ("fcntl", "flock"),
        ("fcntl", "lockf"),
        ("httpx2", "Client"),
    }
)


def _receiver_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _receiver_name(node.func)
    return ""


def _repository_names(tree: ast.AST) -> frozenset[str]:
    """Names bound to a ``*Repository``-typed parameter, and ``self.x = param``."""
    parameters: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.arg)
            and node.annotation is not None
            and "Repository" in ast.unparse(node.annotation)
        ):
            parameters.add(node.arg)
    attributes = {
        target.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Name)
        and node.value.id in parameters
        for target in node.targets
        if isinstance(target, ast.Attribute)
    }
    return frozenset(parameters | attributes)


def _is_sync_io(call: ast.Call, repositories: frozenset[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in _BARE_CALLS or bool(_RECEIVER.search(func.id))
    if not isinstance(func, ast.Attribute):
        return False
    if isinstance(func.value, ast.Name) and (func.value.id, func.attr) in _DOTTED_CALLS:
        return True
    if func.attr in {"flock", "fsync"} or _RECEIVER.search(func.attr):
        return True
    receiver = _receiver_name(func.value)
    return bool(_RECEIVER.search(receiver)) or receiver in repositories


def _reason(lines: list[str], call: ast.Call) -> str | None:
    """The ``# sequential:`` reason on the call's lines or the line above."""
    first = call.lineno - 1
    last = (call.end_lineno or call.lineno) - 1
    for index in range(max(first - 1, 0), last + 1):
        match = MARKER.search(lines[index])
        if match:
            return match.group(1)
    return None


def _calls_on_the_loop(function: ast.AsyncFunctionDef) -> list[ast.Call]:
    """Calls the coroutine itself runs: not awaited, not in a nested def."""
    found: list[ast.Call] = []

    def visit(node: ast.AST, awaited: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child,
                ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef,
            ):
                continue
            if isinstance(child, ast.Call) and not awaited:
                found.append(child)
            visit(child, isinstance(child, ast.Await))

    visit(function, False)
    return found


def scan_source(source: str) -> list[tuple[int, str]]:
    lines = source.splitlines()
    hits: dict[int, str] = {}
    tree = ast.parse(source)
    repositories = _repository_names(tree)
    for function in ast.walk(tree):
        if not isinstance(function, ast.AsyncFunctionDef):
            continue
        # A `with session():` item is a call on the loop; the body is scanned
        # through the same walk.
        for call in _calls_on_the_loop(function):
            if not _is_sync_io(call, repositories):
                continue
            reason = _reason(lines, call)
            if reason in REASONS:
                continue
            hits[call.lineno] = KIND if reason is None else BAD_REASON_KIND
    return sorted(hits.items())
