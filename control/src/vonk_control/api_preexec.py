"""Unprivileged database initialization before the control API becomes PID 1."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from vonk_agent_protocol.outcome import UnknownOutcomeError
from vonk_control.db.connections import (
    build_engine,
    run_with_database_startup_retry,
    session_factory,
)
from vonk_control.db.startup import initialize_database
from vonk_control.runtime_init import RuntimeSecretError, read_runtime_secret
from vonk_control.security.browser_auth import bootstrap_administrator
from vonk_control.settings import SECRETS_ROOT, install_channel_from_env

_LOGGER = logging.getLogger(__name__)

_API_ID = 10001


def step_down_from_root() -> None:
    """Never run the API as root, even when an older Compose file starts it so.

    A Compose file from before the API ran as 10001 still says ``user: "0:0"``
    (with SETUID/SETGID) until the NAS installer reruns; NasComposeBundleStale
    reports that. Files the API writes into volumes it shares with the 10001
    worker must stay 10001's, so the API steps down and keeps serving. Under
    the current Compose file it starts as 10001 and this does nothing.
    """
    if os.geteuid() != 0:
        return
    _LOGGER.warning(
        "control API started as root by an older Compose file; rerun the NAS installer"
    )
    os.setgroups([])
    os.setgid(_API_ID)
    os.setuid(_API_ID)


def prepare_owned_state() -> None:
    """Initialize the database and the administrator before the API starts.

    ``runtime-init`` has already staged the secrets (the administrator
    password among them), the runtime kit and the shared-volume ownership.
    """
    database_url = (SECRETS_ROOT / "database-url").read_text(encoding="utf-8").strip()
    initialize_database(database_url, channel=install_channel_from_env())
    password_path = SECRETS_ROOT / "admin-password"
    if not password_path.exists():
        # Only an older Compose file stages no password. An existing
        # administrator is unaffected; a start after the rerun bootstraps.
        _LOGGER.warning("administrator password is not staged; bootstrap skipped")
        return
    initialize_administrator(database_url, password_path)


def _staged_admin_password(raw: bytes) -> str | None:
    """The one-line UTF-8 password the file holds, or None when it is unusable."""
    if not raw.endswith(b"\n") or raw[:-1].find(b"\n") >= 0 or b"\0" in raw:
        return None
    try:
        return raw[:-1].decode("utf-8")
    except UnicodeDecodeError:
        return None


def initialize_administrator(database_url: str, password_path: Path) -> None:
    """Bootstrap from the staged file; an unusable file never ends startup.

    The file is the administrator reset path, so a hand edit that breaks it
    keeps the stored verifier and is logged instead of crash-looping the API.
    A transient read fault still propagates: the bounded startup retries it.
    """
    try:
        raw = read_runtime_secret(password_path, maximum_bytes=257)
    except RuntimeSecretError:
        raw = b""
    password = _staged_admin_password(raw)
    if password is None:
        _LOGGER.error(
            "staged administrator password is unusable (it must be one line "
            "of UTF-8 text ending in a newline, at most 256 bytes); "
            "the stored password is kept"
        )
        return

    def bootstrap_once() -> None:
        engine = build_engine(database_url)
        try:
            bootstrap_administrator(session_factory(engine), password)
        finally:
            engine.dispose()

    run_with_database_startup_retry(bootstrap_once, label="PostgreSQL")


def _exec(command: tuple[str, ...]) -> None:
    os.execvp(command[0], command)  # noqa: S606 -- the exec or pty spawn is intended


def main(
    command: Sequence[str] | None = None,
    *,
    execute: Callable[[tuple[str, ...]], None] = _exec,
) -> None:
    argv = tuple(sys.argv[1:] if command is None else command)
    step_down_from_root()
    for _attempt in range(3):
        try:
            prepare_owned_state()
            break
        except OSError, UnknownOutcomeError:
            _LOGGER.warning(
                "database schema or administrator initialization is unavailable",
                exc_info=True,
            )
    else:
        # This startup owns no persisted lease. Ending leaves all prior files
        # intact; Compose starts a new independently bounded startup request.
        raise SystemExit(1)
    execute(argv)


if __name__ == "__main__":
    main()
