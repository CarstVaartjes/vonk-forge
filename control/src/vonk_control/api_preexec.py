"""Bounded privileged initialization before the control API becomes PID 1."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .db import initialize_database
from .runtime_init import (
    prepare_shared_volumes,
    stage_compose_secrets,
)

_API_UID = 10001
_API_GID = 10001
_SOURCE_SECRETS = Path("/run/secrets")
_NORMALIZED_SECRETS = Path("/normalized")


def prepare_owned_state() -> None:
    """Initialize all state formerly owned by the bootstrap helper."""
    if os.geteuid() != 0:
        raise RuntimeError("control API pre-exec must start as root")
    stage_compose_secrets(_SOURCE_SECRETS, _NORMALIZED_SECRETS)
    prepare_shared_volumes()
    database_url = (_NORMALIZED_SECRETS / "database-url").read_text(
        encoding="utf-8"
    ).strip()
    initialize_database(database_url)


def _probe_source_secrets(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    os.close(descriptor)


def _exec(command: tuple[str, ...]) -> None:
    os.execvp(command[0], command)


def drop_runtime_privileges(
    *,
    source_secrets: Path = _SOURCE_SECRETS,
    source_probe: Callable[[Path], None] = _probe_source_secrets,
) -> None:
    """Irreversibly become the API identity and verify secret isolation."""
    os.setgroups([])
    os.setresgid(_API_GID, _API_GID, _API_GID)
    os.setresuid(_API_UID, _API_UID, _API_UID)
    if os.getgroups() or os.getresgid() != (_API_GID,) * 3:
        raise RuntimeError("control API group privileges were not dropped")
    if os.getresuid() != (_API_UID,) * 3:
        raise RuntimeError("control API user privileges were not dropped")
    try:
        source_probe(Path(source_secrets))
    except PermissionError:
        pass
    else:
        raise RuntimeError("control API can still traverse source secrets")


def drop_privileges_and_exec(
    command: Sequence[str],
    *,
    source_secrets: Path = _SOURCE_SECRETS,
    source_probe: Callable[[Path], None] = _probe_source_secrets,
    execute: Callable[[tuple[str, ...]], None] = _exec,
) -> None:
    """Irreversibly become the API identity, verify isolation, and exec."""
    argv = tuple(command)
    if not argv or not all(isinstance(argument, str) and argument for argument in argv):
        raise RuntimeError("control API command is invalid")
    drop_runtime_privileges(
        source_secrets=source_secrets,
        source_probe=source_probe,
    )
    execute(argv)
    raise RuntimeError("control API exec returned unexpectedly")


def main(command: Sequence[str] | None = None) -> None:
    argv = tuple(sys.argv[1:] if command is None else command)
    prepare_owned_state()
    drop_privileges_and_exec(argv)


if __name__ == "__main__":
    main()
