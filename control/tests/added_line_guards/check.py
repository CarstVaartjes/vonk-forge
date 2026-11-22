"""Run every added-line mode over one source file."""

from __future__ import annotations

import ast
from collections.abc import Sequence

from .. import optimistic_boundaries as optimistic
from .context import CheckContext
from .registry import BLOCKS
from .scans import (
    MODES,
    shim_hits,
)


def check_source(
    path: str,
    source: str,
    added: set[int],
    *,
    modes: Sequence[str] = MODES,
    words: frozenset[str] | None = None,
    removed: Sequence[str] = (),
) -> list[str]:
    """Old findings cannot fail the change; only a reported added line can.

    ``removed`` is the file's removed text from the same patch.
    """
    if not added or "generated_control" in path or "/generated/" in path:
        return []
    is_test = (
        "/tests/" in path
        # Browser fixtures (control/web/e2e) hold the wire documents, like the .spec files.
        or path.startswith(("tests/", "control/web/e2e/"))
        or path.endswith(
            ("/tests.rs", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
        )
    )
    noexit_hits: list[str] = (
        shim_hits(path, source, added) if "maintainershim" in modes else []
    )
    python = path.endswith(".py") or (
        source.startswith("#!") and "python" in source.splitlines()[0]
    )
    hits: list[tuple[int, str]] = []
    ctx = CheckContext(
        path=path,
        source=source,
        added=added,
        modes=modes,
        words=words,
        removed=removed,
        is_test=is_test,
        python=python,
        hits=hits,
        noexit_hits=noexit_hits,
    )
    for block in BLOCKS:
        block(ctx)
    if python:
        tree = ast.parse(source)
        spans = [
            (node.lineno, node.end_lineno or node.lineno)
            for node in ast.walk(tree)
            if isinstance(
                node,
                (
                    ast.Subscript,
                    ast.Compare,
                    ast.Call,
                    ast.Raise,
                    ast.Constant,
                    ast.ExceptHandler,
                ),
            )
        ]
        hits = [
            (
                line
                if kind in {optimistic.KIND, optimistic.WRITE_KIND}
                else next(
                    (
                        changed
                        for changed in sorted(added)
                        if any(
                            start == line and start <= changed <= end
                            for start, end in spans
                        )
                    ),
                    line,
                ),
                kind,
            )
            for line, kind in hits
        ]
    return sorted(
        set(noexit_hits)
        | {f"{path}:{line}: {kind}" for line, kind in hits if line in added}
    )
