"""Process locks shared by qualification campaigns regardless of ledger path."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .fleet_qualification import QualificationError


def _default_lock_directory() -> Path:
    configured = os.environ.get("VONK_QUALIFICATION_LOCK_DIR")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise QualificationError(
                "VONK_QUALIFICATION_LOCK_DIR must be an absolute path"
            )
        return path
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        root = Path(state_home)
        if not root.is_absolute():
            raise QualificationError("XDG_STATE_HOME must be an absolute path")
    else:
        root = Path.home() / ".local" / "state"
    return root / "vonk-forge" / "qualification-locks"


def _prepare_lock_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    getuid = getattr(os, "getuid", None)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (getuid is not None and metadata.st_uid != getuid())
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise QualificationError("qualification node-lock directory is not private")


def _node_lock_path(lock_directory: Path, node_id: str) -> Path:
    digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()
    return lock_directory / f"node-{digest}.lock"


@contextmanager
def ledger_lock(path: Path) -> Iterator[None]:
    """Exclusively lock one evidence ledger without following a lock symlink."""

    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise QualificationError("qualification lock cannot be opened safely")
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | no_follow,
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        getuid = getattr(os, "getuid", None)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (getuid is not None and metadata.st_uid != getuid())
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise QualificationError("qualification lock is not private")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise QualificationError(
                f"another qualification runner owns {lock_path}"
            ) from error
        yield
    finally:
        os.close(descriptor)


@contextmanager
def node_locks(
    node_ids: Sequence[str], *, lock_directory: Path | None = None
) -> Iterator[None]:
    """Exclusively lock exact controller node IDs for the caller's lifetime."""

    exact_node_ids = sorted(set(node_ids))
    if not exact_node_ids:
        yield
        return
    if any(not node_id for node_id in exact_node_ids):
        raise QualificationError("qualification node lock requires an exact node ID")

    directory = lock_directory or _default_lock_directory()
    _prepare_lock_directory(directory)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise QualificationError("qualification node lock cannot be opened safely")

    descriptors: list[int] = []
    try:
        for node_id in exact_node_ids:
            path = _node_lock_path(directory, node_id)
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | no_follow,
                0o600,
            )
            try:
                metadata = os.fstat(descriptor)
                getuid = getattr(os, "getuid", None)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or (getuid is not None and metadata.st_uid != getuid())
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                ):
                    raise QualificationError(
                        f"qualification node lock is not private: {node_id}"
                    )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise QualificationError(
                        f"another qualification campaign owns controller node {node_id}"
                    ) from error
            except Exception:
                os.close(descriptor)
                raise
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
