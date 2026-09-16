"""Static audit of the coordination boundaries in ``control/src``.

``docs/architecture-overview.md`` owns the rules. This module is the machine
check for the two that can be proven from the syntax tree:

* a SQL transaction never spans external work (filesystem or managed-storage
  access, HTTP, process execution, subprocess work, child completion, or retry
  sleep); and
* at most one artifact lock is held at a time, it is acquired nonblockingly,
  and it is never acquired inside a SQL transaction.

A transaction scope is a ``with`` block whose context expression opens a
session or a transaction on a session/engine. An *artifact lock* is a local
mutual-exclusion object or an ``fcntl.flock`` acquisition; a ``LOCK_NB``
acquisition is nonblocking and is not a violation on its own.

In-memory computation is not external work. Hashing bytes that are already in
memory, canonical JSON encoding, Pydantic validation, and value-object
``datetime.replace`` are not scanned. Storage work is observed through the call
that reaches it: ``open``, ``read_bytes``, ``stat``, ``verify_path``, and the
project helpers this module lists explicitly.

``tools/coordination-baseline.json`` records every site the current revision
still violates. The gate fails on a site the baseline does not name and on a
baseline entry whose site is gone, so the baseline can only shrink. A site is
matched on its full identity -- path, line, kind, function, and detail -- so
replacing one violation with a different call in the same function cannot keep
a stale entry green.

The gate is only meaningful if the scanner fails on the wrong implementation,
so ``test_coordination_boundaries.py`` runs it against fixtures that contain
each violation and against fixtures that contain each allowed shape.
"""

from __future__ import annotations

import ast
import json
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_SOURCE_ROOT = REPO_ROOT / "control" / "src"
BASELINE_PATH = REPO_ROOT / "tools" / "coordination-baseline.json"

# One site kind per provable rule. The kind is part of the site identity, so a
# site cannot change kind and keep its baseline entry.
SQL_TRANSACTION_SPANS_EXTERNAL_WORK = "sql_transaction_spans_external_work"
SQL_TRANSACTION_SPANS_ARTIFACT_LOCK = "sql_transaction_spans_artifact_lock"
BLOCKING_ARTIFACT_LOCK = "blocking_artifact_lock"
NESTED_ARTIFACT_LOCK = "nested_artifact_lock"

KINDS = frozenset(
    {
        SQL_TRANSACTION_SPANS_EXTERNAL_WORK,
        SQL_TRANSACTION_SPANS_ARTIFACT_LOCK,
        BLOCKING_ARTIFACT_LOCK,
        NESTED_ARTIFACT_LOCK,
    }
)

_SESSION_FACTORY_TAILS = frozenset(
    {
        "session",
        "sessions",
        "_session",
        "_sessions",
        "session_factory",
        "_session_factory",
        "sessionmaker",
    }
)
_TRANSACTION_TAILS = frozenset({"begin", "begin_nested"})

# Names that reach managed storage, the network, a process, or a wait. The
# list is deliberately explicit: a new helper that performs external work has
# to be named here, which is the review point for this gate.
_EXTERNAL_TAILS = frozenset(
    {
        # Filesystem observation and mutation.
        "stat",
        "lstat",
        "fstat",
        "listdir",
        "scandir",
        "walk",
        "open",
        "unlink",
        "rename",
        "mkdir",
        "makedirs",
        "rmdir",
        "symlink",
        "link",
        "chmod",
        "chown",
        "fsync",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "iterdir",
        "glob",
        "rglob",
        "touch",
        "exists",
        "is_file",
        "is_dir",
        "is_symlink",
        "samefile",
        "rmtree",
        "copytree",
        "copyfile",
        "copy2",
        "disk_usage",
        # Content verification of stored objects.
        "verify_path",
        "verify_existing",
        # Process execution, off-thread work, and bounded waiting.
        "Popen",
        "check_call",
        "check_output",
        "to_thread",
        "sleep",
        "wait",
        "wait_for",
        "join",
        # Project helpers that read or write managed storage.
        "_succeeded_build_available",
        "_cached_build_receipt",
        "_published_receipt_authorizes",
        "_commit_recipe_image_upload",
        "_sha256_path",
        "_managed_cached_objects",
        "_read_verified_artifact",
    }
)
_EXTERNAL_PREFIXES = (
    "subprocess.",
    "shutil.",
    "socket.",
    "httpx.",
    "requests.",
    "urllib.",
    "os.",
    "pathlib.",
)
# ``replace``, ``join``, and ``remove`` are also value-object or collection
# methods. They are external work only when the receiver names a filesystem
# path, directory, or file; ``row.occurred_at.replace`` is in-memory.
_AMBIGUOUS_TAILS = frozenset({"replace", "join", "remove", "resolve", "mkdir"})
_PATH_RECEIVER_SEGMENTS = frozenset(
    {
        "path",
        "paths",
        "root",
        "directory",
        "dir",
        "file",
        "files",
        "archive",
        "object",
        "partial",
        "destination",
        "source",
        "target",
        "selector",
        "receipt",
        "manifest",
        "artifact",
        "blob",
        "store",
        "cache",
        "cache_root",
        "object_root",
        "partial_root",
        "image_root",
        "staging",
        "tempdir",
        "tmp",
        "tmp_path",
    }
)

_NON_LOCK_TAILS = frozenset({"unlock", "locked", "blocking", "block"})

# The architecture's artifact-lock rules are about the locks that guard
# managed-storage bytes: at most one held at a time, acquired outside SQL, and
# never blocking. An in-process concurrency guard is a different resource
# class -- it serialises this process's own work (one identity's preparation,
# the reservation quota) and is *expected* to be held while a narrower artifact
# lock is taken, because the artifact lock is what keeps the guarded work
# exclusive. Every name here is reviewed and must stay justified in the plan:
# a guard that is not listed is still scanned as an artifact lock, so a new
# lock cannot silently opt out.
GUARD_LOCK_NAMES = frozenset(
    {
        "_identity_lock",
        "_identity_locks_guard",
        "_quota_lock",
    }
)

# The reviewed reason recorded for each site kind still in the baseline. A new
# site of an existing kind inherits the reason for that kind; a site that no
# longer occurs must be deleted from the baseline, so these reasons can only
# disappear as the corresponding work lands.
DEFAULT_REASONS = {
    BLOCKING_ARTIFACT_LOCK: (
        "Reviewed deferral: this file lock is a kernel-level mutual exclusion "
        "held across a potentially slow operation. Replacing it with a "
        "nonblocking claim changes the caller into a reschedule loop, so it "
        "lands with the reservation and bounded-retry protocol in its own "
        "package rather than as a flag change here."
    ),
    NESTED_ARTIFACT_LOCK: (
        "Reviewed deferral: two artifact locks are held at once. Removing the "
        "outer one changes which writer wins the contested resource, so the "
        "lock set is reduced in its own package with its contention tests."
    ),
}


def _dotted_name(node: ast.AST) -> str | None:
    """Render a ``Name``/``Attribute`` chain as a dotted string."""

    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    else:
        return None
    return ".".join(reversed(parts))


def _tail(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _transaction_context(expr: ast.expr) -> str | None:
    """Return the call that opens a SQL transaction, or ``None``."""

    if not isinstance(expr, ast.Call):
        return None
    callee = _dotted_name(expr.func)
    if callee is None:
        return None
    tail = _tail(callee)
    if tail in _SESSION_FACTORY_TAILS:
        return callee
    if tail in _TRANSACTION_TAILS:
        receiver = callee[: -len(tail) - 1] if "." in callee else ""
        receiver_tail = _tail(receiver).lower()
        if (
            receiver_tail in _SESSION_FACTORY_TAILS
            or "session" in receiver_tail
            or receiver_tail == "engine"
        ):
            return callee
    return None


def _lock_name(expr: ast.expr) -> str | None:
    """Return the local lock context manager name, or ``None``."""

    if isinstance(expr, ast.Call):
        name = _dotted_name(expr.func)
    elif isinstance(expr, (ast.Name, ast.Attribute)):
        name = _dotted_name(expr)
    else:
        return None
    if name is None:
        return None
    tail = _tail(name).lower()
    if tail in _NON_LOCK_TAILS:
        return None
    if tail == "flock" or "lock" in tail:
        return name
    return None


def _is_guard_lock(name: str) -> bool:
    return _tail(name) in GUARD_LOCK_NAMES


def _artifact_lock(expr: ast.expr) -> str | None:
    """Return the artifact-lock name only when ``expr`` is not a guard."""

    name = _lock_name(expr)
    if name is None or _is_guard_lock(name):
        return None
    return name


def _flock_flags(call: ast.Call) -> frozenset[str]:
    return frozenset(
        child.attr
        for child in ast.walk(call)
        if isinstance(child, ast.Attribute) and child.attr.startswith("LOCK_")
    )


_ACQUIRE_FLAGS = frozenset({"LOCK_EX", "LOCK_SH"})


def _acquires_flock(expr: ast.expr) -> ast.Call | None:
    if not isinstance(expr, ast.Call):
        return None
    if _tail(_dotted_name(expr.func) or "") != "flock":
        return None
    return expr if _flock_flags(expr) & _ACQUIRE_FLAGS else None


def _is_blocking_flock(call: ast.Call) -> bool:
    flags = _flock_flags(call)
    return bool(flags & _ACQUIRE_FLAGS) and "LOCK_NB" not in flags


def _exclusive_create_descriptors(body: Sequence[ast.stmt]) -> set[str]:
    """Names bound to a descriptor from an exclusive-create ``os.open``.

    ``os.open(..., os.O_EXCL)`` either creates the file or fails, so the
    descriptor returned to this call names a file no other process can already
    hold open. A lock on such a descriptor is private by construction: it can
    never contend, whatever flags it is taken with.
    """

    created: set[str] = set()
    for statement in body:
        for child in ast.walk(statement):
            if not isinstance(child, ast.Assign) or len(child.targets) != 1:
                continue
            value = child.value
            if not isinstance(value, ast.Call):
                continue
            if _tail(_dotted_name(value.func) or "") != "open":
                continue
            # The flag is usually one term of a ``|`` chain, so search the
            # whole call rather than its direct arguments.
            if any(
                isinstance(child, ast.Attribute) and child.attr == "O_EXCL"
                for child in ast.walk(value)
            ):
                target = child.targets[0]
                if isinstance(target, ast.Name):
                    created.add(target.id)
    return created


def _flock_descriptor_name(call: ast.Call) -> str | None:
    """The plain name whose descriptor a ``flock`` call locks, if any."""

    if not call.args:
        return None
    first = call.args[0]
    return first.id if isinstance(first, ast.Name) else None


def _receiver_is_path(name: str) -> bool:
    """Whether a dotted call name's receiver names a filesystem location."""

    receiver = name.rsplit(".", 1)[0] if "." in name else ""
    segments = {segment.lower() for segment in receiver.split(".")}
    return bool(segments & _PATH_RECEIVER_SEGMENTS)


def _external_call_name(call: ast.Call) -> str | None:
    """Return the external-work call name, or ``None`` for database-only code."""

    name = _dotted_name(call.func)
    if name is None:
        return None
    if name.startswith(_EXTERNAL_PREFIXES):
        return name
    tail = _tail(name)
    if tail in _AMBIGUOUS_TAILS and not _receiver_is_path(name):
        return None
    if tail in _EXTERNAL_TAILS:
        return name
    return None


@dataclass(frozen=True)
class Site:
    """One provable coordination violation."""

    path: str
    line: int
    kind: str
    function: str
    detail: str

    @property
    def identity(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "kind": self.kind,
            "function": self.function,
            "detail": self.detail,
        }

    def render(self) -> str:
        return (
            f"{self.path}:{self.line}: {self.kind} in {self.function} ({self.detail})"
        )


class _FunctionScanner:
    """One pass over a single function body, innermost owner wins."""

    def __init__(
        self, path: str, function: str, statements: Sequence[ast.stmt]
    ) -> None:
        self._path = path
        self._function = function
        self._transaction_depth = 0
        self._held_locks: list[str] = []
        self._sites: list[Site] = []
        self._seen: set[tuple[str, int, str]] = set()
        self._private_descriptors = _exclusive_create_descriptors(statements)

    def _report(self, line: int, kind: str, detail: str) -> None:
        key = (kind, line, detail)
        if key in self._seen:
            return
        self._seen.add(key)
        self._sites.append(
            Site(
                path=self._path,
                line=line,
                kind=kind,
                function=self._function,
                detail=detail,
            )
        )

    def run(self, node: ast.AST) -> list[Site]:
        self._walk_statements(getattr(node, "body", []))
        return self._sites

    def _walk_statements(self, statements: Sequence[ast.AST]) -> None:
        for statement in statements:
            if isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            ):
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                self._walk_with(statement)
                continue
            if isinstance(statement, ast.Call):
                self._observe_call(statement)
            for attribute in ("body", "orelse", "finalbody"):
                nested = getattr(statement, attribute, None)
                if isinstance(nested, list):
                    self._walk_statements(nested)
            self._walk_expressions(statement)

    def _walk_expressions(self, node: ast.AST) -> None:
        """Observe every nested call, including calls used as arguments."""

        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                self._observe_call(child)
            self._walk_expressions(child)

    def _walk_with(self, node: ast.With | ast.AsyncWith) -> None:
        # A lock and a transaction in the same ``with`` are acquired together,
        # which is the one allowed reverse edge: a short fence transaction sits
        # inside the held lock. Only a lock acquired from inside an already-open
        # transaction body is the forbidden SQL-to-filesystem edge.
        transaction = any(
            _transaction_context(item.context_expr) is not None for item in node.items
        )
        locks: list[tuple[int, str, bool]] = []
        for item in node.items:
            name = _artifact_lock(item.context_expr)
            if name is not None:
                locks.append((item.context_expr.lineno, name, False))
            flock = _acquires_flock(item.context_expr)
            if flock is not None:
                locks.append((flock.lineno, "fcntl.flock", self._flock_blocks(flock)))
        for line, name, blocking in locks:
            if self._transaction_depth:
                self._report(
                    line,
                    SQL_TRANSACTION_SPANS_ARTIFACT_LOCK,
                    f"artifact lock {name}",
                )
            if self._held_locks:
                self._report(
                    line,
                    NESTED_ARTIFACT_LOCK,
                    "artifact locks held together: "
                    + ", ".join([*self._held_locks, name]),
                )
            if blocking:
                self._report(
                    line,
                    BLOCKING_ARTIFACT_LOCK,
                    f"blocking artifact lock {name}",
                )
        if transaction:
            self._transaction_depth += 1
        self._held_locks.extend(name for _line, name, _blocking in locks)
        self._walk_statements(node.body)
        if transaction:
            self._transaction_depth -= 1
        del self._held_locks[len(self._held_locks) - len(locks) :]

    def _flock_blocks(self, flock: ast.Call) -> bool:
        """Whether a blocking flock can actually contend.

        A flock on a descriptor from an exclusive create is private by
        construction, so its flags cannot make it block on another holder.
        """

        descriptor = _flock_descriptor_name(flock)
        if descriptor is not None and descriptor in self._private_descriptors:
            return False
        return _is_blocking_flock(flock)

    def _observe_call(self, call: ast.Call) -> None:
        flock = _acquires_flock(call)
        if flock is not None:
            name = "fcntl.flock"
            if self._transaction_depth:
                self._report(
                    call.lineno,
                    SQL_TRANSACTION_SPANS_ARTIFACT_LOCK,
                    f"artifact lock {name}",
                )
            if self._held_locks:
                self._report(
                    call.lineno,
                    NESTED_ARTIFACT_LOCK,
                    "artifact locks held together: "
                    + ", ".join([*self._held_locks, name]),
                )
            if self._flock_blocks(flock):
                self._report(
                    call.lineno,
                    BLOCKING_ARTIFACT_LOCK,
                    f"blocking artifact lock {name}",
                )
        if self._transaction_depth:
            name = _external_call_name(call)
            if name is not None:
                self._report(
                    call.lineno,
                    SQL_TRANSACTION_SPANS_EXTERNAL_WORK,
                    f"external call {name}",
                )


def _function_sites(
    path: str, function: ast.FunctionDef | ast.AsyncFunctionDef
) -> list[Site]:
    return _FunctionScanner(path, function.name, function.body).run(function)


def scan_source(source: str, *, path: str) -> list[Site]:
    """Return every provable coordination violation in one module's source."""

    tree = ast.parse(source)

    def walk(node: ast.AST) -> Iterator[Site]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from _function_sites(path, child)
            elif isinstance(child, ast.Lambda):
                continue
            else:
                yield from walk(child)

    sites = list(walk(tree))
    return sorted(
        {_key(site.identity): site for site in sites}.values(),
        key=lambda site: (site.line, site.kind, site.detail),
    )


def scan_coordination_sites(root: Path = CONTROL_SOURCE_ROOT) -> list[Site]:
    """Return every provable coordination violation under ``root``."""

    sites: list[Site] = []
    for module in sorted(root.rglob("*.py")):
        relative = module.relative_to(REPO_ROOT).as_posix()
        sites.extend(scan_source(module.read_text(encoding="utf-8"), path=relative))
    return sorted(
        sites, key=lambda site: (site.path, site.line, site.kind, site.detail)
    )


def _baseline_identity(entry: object, where: str) -> dict[str, object]:
    if not isinstance(entry, dict):
        raise TypeError(f"{where}: baseline entry is not an object")
    identity: dict[str, object] = {}
    for field in ("path", "line", "kind", "function", "detail"):
        value = entry.get(field)
        if field == "line":
            if type(value) is not int:
                raise TypeError(f"{where}: baseline line must be an integer")
        elif not isinstance(value, str):
            raise TypeError(f"{where}: baseline {field} must be a string")
        identity[field] = value
    if identity["kind"] not in KINDS:
        raise ValueError(f"{where}: unknown site kind {identity['kind']!r}")
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"{where}: baseline entry needs a written reason")
    return identity


def load_baseline(path: Path = BASELINE_PATH) -> list[dict[str, object]]:
    """Read the reviewed baseline. A malformed baseline is a hard failure."""

    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError(f"{path}: baseline must be a schema-1 object")
    if document.get("schema") != 1:
        raise ValueError(f"{path}: baseline must be a schema-1 object")
    entries = document.get("sites")
    if not isinstance(entries, list):
        raise TypeError(f"{path}: baseline needs a sites array")
    return [
        _baseline_identity(entry, f"{path} sites[{index}]")
        for index, entry in enumerate(entries)
    ]


def _key(identity: dict[str, object]) -> tuple[object, ...]:
    return (
        identity["path"],
        identity["line"],
        identity["kind"],
        identity["function"],
        identity["detail"],
    )


def evaluate_coordination_gate(
    sites: Sequence[Site], baseline: Sequence[dict[str, object]]
) -> list[str]:
    """Return one message per new or stale site. An empty list is a pass."""

    messages: list[str] = []
    known = {_key(entry) for entry in baseline}
    current: dict[tuple[object, ...], Site] = {}
    for site in sites:
        current.setdefault(_key(site.identity), site)
    for key, site in current.items():
        if key not in known:
            messages.append(f"new coordination violation: {site.render()}")
    for entry in baseline:
        if _key(entry) not in current:
            messages.append(
                "baseline entry no longer occurs; delete it: "
                f"{entry['path']}:{entry['line']}: {entry['kind']} "
                f"in {entry['function']}"
            )
    return messages


def render_baseline(sites: Sequence[Site], reasons: dict[str, str]) -> str:
    """Render a deterministic baseline document for ``sites``."""

    entries = []
    for site in sorted(sites, key=lambda item: _key(item.identity)):
        document = site.identity
        document["reason"] = reasons.get(site.kind, "")
        entries.append(document)
    return json.dumps({"schema": 1, "sites": entries}, indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    sites = scan_coordination_sites()
    if arguments and arguments[0] == "--write-baseline":
        reasons: dict[str, str] = dict(DEFAULT_REASONS)
        if BASELINE_PATH.exists():
            for entry in load_baseline(BASELINE_PATH):
                reasons.setdefault(str(entry["kind"]), str(entry.get("reason", "")))
        BASELINE_PATH.write_text(render_baseline(sites, reasons), encoding="utf-8")
        print(f"wrote {len(sites)} sites to {BASELINE_PATH}")
        return 0
    messages = evaluate_coordination_gate(sites, load_baseline())
    if messages:
        for message in messages:
            print(message, file=sys.stderr)
        return 1
    print(f"coordination boundaries hold at {len(sites)} reviewed sites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
