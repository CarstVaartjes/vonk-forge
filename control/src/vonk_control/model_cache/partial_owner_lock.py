"""The one lock every writer of a ``partials/<owner>`` directory shares.

A transfer holds it shared while it writes into the directory; the partials
collector (and a set removal, for a set-digest owner) holds it exclusive while it
removes the directory. Neither side waits: a busy lock is a writer or a remover
at work, so the caller defers and asks again.
"""

from __future__ import annotations

import fcntl
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import _ArtifactWriterBusy

# Concurrent first creation of one lock file by pool threads can fail spuriously
# with ENOENT on some filesystems (APFS); creation is therefore serialized.
_CREATE = threading.Lock()


@contextmanager
def partial_owner_lock(root: Path, owner: str, *, exclusive: bool) -> Iterator[None]:
    """Hold ``locks/model-set-<owner>`` without waiting for its other holders."""

    directory_fd = os.open(
        root / "locks",
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        with _CREATE:
            descriptor = os.open(
                f"model-set-{owner}",
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
    finally:
        os.close(directory_fd)
    with os.fdopen(descriptor, "a+b") as lock_file:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(lock_file.fileno(), mode | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _ArtifactWriterBusy(owner) from None
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
