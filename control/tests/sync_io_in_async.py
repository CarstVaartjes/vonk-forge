"""Find synchronous I/O run directly inside an ``async def``.

The API is one uvicorn process, so a session or repository call, a file open or
read, an fsync, a flock, ``time.sleep``, a subprocess or an ``httpx2.Client``
call made on the event loop freezes every other request until it returns. Run
the sync segment with ``await asyncio.to_thread(fn, *args)`` (or
``run_in_threadpool``), or make the handler a plain ``def`` so FastAPI runs it
in its threadpool.

The only exemption is ``# sequential: not-hot`` on the call's lines or the
line above, for startup, shutdown or one-off code. The other P21 reasons
(data-dependency, ordering, one-session, one-resource) justify running steps in
sequence, not blocking the loop, so they do not exempt here.

Lexical, one module at a time: an unawaited call to a sync ``def`` (or
``@contextmanager``) of the same module whose body has a hit is itself a hit,
followed through further same-module calls. I/O reached through another
module's helper is not seen, and the bodies of nested ``def``/``lambda`` are
skipped because they run where they are called. Each async function with a hit
also reports it on its ``async def`` line, so adding or editing that line (for
example turning a ``def`` into an ``async def``) carries the findings.
"""

from __future__ import annotations

import ast
import re

KIND = (
    "sync I/O inside async def; use await asyncio.to_thread(...) or, for "
    "startup/shutdown/one-off code, add '# sequential: not-hot'"
)
BAD_REASON_KIND = (
    "'# sequential:' blocks the event loop only for '# sequential: not-hot' "
    "(startup, shutdown, one-off); other reasons do not exempt"
)
REASONS = frozenset({"not-hot"})
MARKER = re.compile(r"#\s*sequential:\s*([\w-]*)")
# Receivers (the last attribute or name before the method) that are a database
# session factory, a session or a repository; a parameter annotated
# ``...Repository`` (and the attribute it is stored in) is one too.
_RECEIVER = re.compile(
    r"(?:^|_)(?:sessions?|session_factory|repos?|repositor(?:y|ies))$"
)
_BARE_CALLS = frozenset({"open"})
# Methods that read or write a file whatever the receiver (a ``Path``).
_FILE_METHODS = frozenset({"read_text", "read_bytes", "write_text", "write_bytes"})
# Dotted calls that block: (module or object, attribute).
_DOTTED_CALLS = frozenset(
    {
        ("time", "sleep"),
        ("os", "fsync"),
        ("os", "fdatasync"),
        ("fcntl", "flock"),
        ("fcntl", "lockf"),
        ("httpx2", "Client"),
        ("tempfile", "mkstemp"),
        ("os", "replace"),
        ("subprocess", "run"),
        ("subprocess", "call"),
        ("subprocess", "check_call"),
        ("subprocess", "check_output"),
        ("subprocess", "Popen"),
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
    if func.attr in {"flock", "fsync"} | _FILE_METHODS or _RECEIVER.search(func.attr):
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


def _calls_on_the_loop(
    function: ast.AsyncFunctionDef | ast.FunctionDef,
) -> list[ast.Call]:
    """Calls the function itself runs: not awaited, not in a nested def."""
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


def _local_name(call: ast.Call) -> str:
    """``f`` for ``f(...)``, ``m`` for ``self.m(...)``/``cls.m(...)``, else ``""``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in {"self", "cls"}
    ):
        return func.attr
    return ""


def _unmarked_hits(
    function: ast.AsyncFunctionDef | ast.FunctionDef,
    lines: list[str],
    repositories: frozenset[str],
    tainted: frozenset[str],
) -> dict[int, str]:
    hits: dict[int, str] = {}
    for call in _calls_on_the_loop(function):
        if not (_is_sync_io(call, repositories) or _local_name(call) in tainted):
            continue
        reason = _reason(lines, call)
        if reason in REASONS:
            continue
        hits[call.lineno] = KIND if reason is None else BAD_REASON_KIND
    return hits


def _tainted_sync_defs(
    tree: ast.AST, lines: list[str], repositories: frozenset[str]
) -> frozenset[str]:
    """Names of the module's sync defs whose body has a hit, transitively."""
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
    tainted: frozenset[str] = frozenset()
    while True:
        grown = tainted | {
            function.name
            for function in functions
            if _unmarked_hits(function, lines, repositories, tainted)
        }
        if grown == tainted:
            return tainted
        tainted = grown


def scan_source(source: str) -> list[tuple[int, str]]:
    lines = source.splitlines()
    tree = ast.parse(source)
    repositories = _repository_names(tree)
    tainted = _tainted_sync_defs(tree, lines, repositories)
    hits: dict[int, str] = {}
    for function in ast.walk(tree):
        if not isinstance(function, ast.AsyncFunctionDef):
            continue
        found = _unmarked_hits(function, lines, repositories, tainted)
        if found:
            hits.update(found)
            # The signature line carries the body's findings.
            hits.setdefault(function.lineno, KIND)
    return sorted(hits.items())
