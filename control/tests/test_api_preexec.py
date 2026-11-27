from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
from vonk_control import api_preexec
from vonk_control.categorized_errors import UnsettledOutcome
from vonk_control.model_primitives import Base
from vonk_control.models.fleet import User
from vonk_control.security.browser_auth import (
    BrowserAuthService,
)


@pytest.mark.parametrize("euid", [0, 10001])
def test_startup_never_runs_the_api_as_root(
    monkeypatch: pytest.MonkeyPatch, euid: int
) -> None:
    # Catches an older Compose file (user 0:0) leaving the API root, which
    # would write root-owned files into volumes the 10001 worker shares.

    events: list[object] = []
    monkeypatch.setattr(api_preexec.os, "geteuid", lambda: euid)
    monkeypatch.setattr(
        api_preexec.os, "setgroups", lambda groups: events.append(("groups", groups))
    )
    monkeypatch.setattr(
        api_preexec.os, "setgid", lambda gid: events.append(("gid", gid))
    )
    monkeypatch.setattr(
        api_preexec.os, "setuid", lambda uid: events.append(("uid", uid))
    )
    monkeypatch.setattr(
        api_preexec, "prepare_owned_state", lambda: events.append("prepare")
    )

    api_preexec.main(("python", "-m", "vonk_control.api"), execute=events.append)

    step_down = [("groups", []), ("gid", 10001), ("uid", 10001)] if euid == 0 else []
    assert events == [*step_down, "prepare", ("python", "-m", "vonk_control.api")]


@pytest.mark.parametrize("staged", [True, False])
def test_owned_state_initializes_database_then_the_single_administrator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, staged: bool
) -> None:
    # Catches the API refusing to start when an older Compose file staged no
    # administrator password (the administrator already exists then).

    (tmp_path / "database-url").write_text("postgresql://control/database\n")
    if staged:
        (tmp_path / "admin-password").write_text("private admin password\n")
    events: list[object] = []
    monkeypatch.setattr(api_preexec, "SECRETS_ROOT", tmp_path)
    monkeypatch.setattr(
        api_preexec,
        "initialize_database",
        lambda database_url, channel: events.append(("database", database_url)),
    )
    monkeypatch.setattr(
        api_preexec,
        "initialize_administrator",
        lambda database_url, password_path: events.append(
            ("administrator", database_url, password_path)
        ),
    )

    api_preexec.prepare_owned_state()

    administrator = (
        "administrator",
        "postgresql://control/database",
        tmp_path / "admin-password",
    )
    assert events == [
        ("database", "postgresql://control/database"),
        *([administrator] if staged else []),
    ]


def test_administrator_initialization_creates_one_login_on_postgres(
    postgres_engine: Engine, tmp_path: Path
) -> None:

    Base.metadata.create_all(postgres_engine)
    password = "correct horse battery staple"
    password_path = tmp_path / "admin-password"
    password_path.write_text(f"{password}\n", encoding="utf-8")
    database_url = postgres_engine.url.render_as_string(hide_password=False)

    api_preexec.initialize_administrator(database_url, password_path)
    api_preexec.initialize_administrator(database_url, password_path)

    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions() as db:
        assert db.scalar(select(User.subject)) == "admin"
        assert db.scalar(select(User.role)) == "administrator"
    auth = BrowserAuthService(
        sessions,
        clock=lambda: datetime.now(UTC),
    )
    assert auth.login("admin", password).identity.actor.subject == "admin"
    with pytest.raises(Exception) as _ending:
        auth.login("admin", "different administrator password")
    assert auth.login("admin", password).identity.actor.subject == "admin"
    with sessions() as db:
        assert tuple(db.scalars(select(User.subject))) == ("admin",)


@pytest.mark.parametrize(
    "content",
    # The three branches: a file the bounded read refuses, a file that is
    # not one terminated line, and a line that is not UTF-8.
    [b"", b"missing final newline", b"\xff not utf-8\n"],
)
def test_an_unusable_staged_password_keeps_startup_going_without_db_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: bytes
) -> None:
    """Catches a hand-edited file crash-looping the API or reaching the database."""

    password_path = tmp_path / "admin-password"
    password_path.write_bytes(content)
    monkeypatch.setattr(
        api_preexec,
        "build_engine",
        lambda _database_url: pytest.fail("malformed secret reached the database"),
    )

    api_preexec.initialize_administrator("postgresql://unused", password_path)


@pytest.mark.parametrize("fault", ["typed", "io"])
def test_startup_storage_unknown_ends_and_fresh_startup_executes(monkeypatch, fault):

    observations = []
    launches = []
    unavailable = [True]

    def observe():
        observations.append(True)
        if unavailable[0]:
            if fault == "io":
                raise OSError("startup volume observation unavailable")
            raise UnsettledOutcome(
                "storage unavailable", reason=WaitReason.OBSERVATION_UNAVAILABLE
            )

    monkeypatch.setattr(api_preexec, "prepare_owned_state", observe)
    monkeypatch.setattr(api_preexec, "step_down_from_root", lambda: None)
    ended = False
    try:
        api_preexec.main(("python",), execute=launches.append)
    except BaseException:  # noqa: BLE001 -- observe ending, not error taxonomy
        ended = True
    assert ended
    assert len(observations) == 3
    assert launches == []
    unavailable[0] = False
    api_preexec.main(("python",), execute=launches.append)
    assert len(observations) == 4
    assert launches == [("python",)]
