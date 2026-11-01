"""Find storage I/O inside a transaction that holds row locks.

A ``with`` over a database session whose body locks rows (``with_for_update``)
keeps those rows locked until it commits, so every file read in it makes the
other workers wait on the disk. Read the facts first, with no transaction open,
and let the locked transaction decide over them. Lexical: I/O reached through
a helper that is not named here is not seen; the list is the guard's scope.
"""

from __future__ import annotations

import ast

KIND = "storage I/O inside a row-locking transaction; read the facts before it"
SESSIONS = frozenset({"begin", "_session", "_sessions", "session_scope", "Session"})
IO_CALLS = frozenset(
    {
        "_object_is_stored",
        "_object_is_available",
        "_stored_object",
        "_stored_object_bytes",
        "_transfer_need",
        "_verified_bytes",
        "_partial_bytes",
        "_running_transfers",
        "scandir",
        "lstat",
        "stat",
        "glob",
        "unused_set_bytes",
        "published_archive_bytes",
    }
)


def _name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _locks(call: ast.Call) -> bool:
    return _name(call) == "with_for_update" or any(
        keyword.arg == "with_for_update" for keyword in call.keywords
    )


def scan_source(source: str) -> list[int]:
    found: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.With) or not any(
            isinstance(item.context_expr, ast.Call)
            and _name(item.context_expr) in SESSIONS
            for item in node.items
        ):
            continue
        calls = [child for child in ast.walk(node) if isinstance(child, ast.Call)]
        if not any(_locks(call) for call in calls):
            continue
        for call in calls:
            name = _name(call)
            if name in IO_CALLS or (
                name == "_transfer_state_for_manifest"
                and not any(keyword.arg == "cached" for keyword in call.keywords)
            ):
                found.add(call.lineno)
    return sorted(found)
