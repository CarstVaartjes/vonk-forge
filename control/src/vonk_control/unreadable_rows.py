"""Say once that a stored row is unreadable; the reader goes on without it.

A damaged or older-contract row is evidence to rebuild (the next sync rewrites
it), never a reason to fail the page or the admission that read it. One place
keeps the once-per-row note for every reader of stored catalog rows.
"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger("vonk_control.library_projection")
LOGGED_UNREADABLE: set[str] = set()


def note_unreadable(kind: str, key: str, detail: str) -> None:
    """Log an unreadable ``kind`` row ``key`` once; the caller reads it as unknown."""

    marker = f"{kind}:{key}"
    if marker not in LOGGED_UNREADABLE:
        LOGGED_UNREADABLE.add(marker)
        LOGGER.warning("ignoring unreadable %s %s: %s", kind, key, detail)
