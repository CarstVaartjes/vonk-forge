from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_control.browser_auth import (
    BrowserAuthenticationError,
    BrowserAuthService,
)
from vonk_control.models import Base, User


class _ExecCalled(RuntimeError):
    pass


def test_privilege_drop_clears_groups_and_saved_root_ids_before_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vonk_control import api_preexec

    events: list[object] = []
    identity = {
        "groups": [0, 10001],
        "gid": (0, 0, 0),
        "uid": (0, 0, 0),
    }

    def setgroups(groups: list[int]) -> None:
        identity["groups"] = list(groups)
        events.append(("groups", tuple(groups)))

    def setresgid(real: int, effective: int, saved: int) -> None:
        identity["gid"] = (real, effective, saved)
        events.append(("gid", real, effective, saved))

    def setresuid(real: int, effective: int, saved: int) -> None:
        identity["uid"] = (real, effective, saved)
        events.append(("uid", real, effective, saved))

    def open_source_secrets(path: Path) -> None:
        events.append(("probe", path))
        if identity["uid"] != (10001, 10001, 10001):
            raise AssertionError("source secrets were probed before the UID drop")
        raise PermissionError

    def exec_process(command: tuple[str, ...]) -> None:
        events.append(("exec", command))
        raise _ExecCalled

    # Linux exposes these calls on the real ``os`` module; retain that patch
    # path there. macOS does not expose setresgid/setresuid, so inject the
    # smallest equivalent boundary instead of skipping the security test.
    if hasattr(api_preexec.os, "setresgid") and hasattr(api_preexec.os, "setresuid"):
        privilege_os = api_preexec.os
        monkeypatch.setattr(privilege_os, "setgroups", setgroups)
        monkeypatch.setattr(privilege_os, "setresgid", setresgid)
        monkeypatch.setattr(privilege_os, "setresuid", setresuid)
        monkeypatch.setattr(privilege_os, "getgroups", lambda: identity["groups"])
        monkeypatch.setattr(privilege_os, "getresgid", lambda: identity["gid"])
        monkeypatch.setattr(privilege_os, "getresuid", lambda: identity["uid"])
    else:
        privilege_os = SimpleNamespace(
            setgroups=setgroups,
            setresgid=setresgid,
            setresuid=setresuid,
            getgroups=lambda: identity["groups"],
            getresgid=lambda: identity["gid"],
            getresuid=lambda: identity["uid"],
        )
        monkeypatch.setattr(api_preexec, "os", privilege_os)

    with pytest.raises(_ExecCalled):
        api_preexec.drop_privileges_and_exec(
            ("python", "-m", "vonk_control.api"),
            source_secrets=Path("/run/secrets"),
            source_probe=open_source_secrets,
            execute=exec_process,
        )

    assert events == [
        ("groups", ()),
        ("gid", 10001, 10001, 10001),
        ("uid", 10001, 10001, 10001),
        ("probe", Path("/run/secrets")),
        ("exec", ("python", "-m", "vonk_control.api")),
    ]


def test_preexec_initializes_owned_state_before_dropping_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vonk_control import api_preexec

    events: list[str] = []
    monkeypatch.setattr(
        api_preexec,
        "prepare_owned_state",
        lambda: events.append("prepare"),
    )
    monkeypatch.setattr(
        api_preexec,
        "drop_privileges_and_exec",
        lambda command: events.append("exec:" + " ".join(command)),
    )

    api_preexec.main(("python", "-m", "vonk_control.api"))

    assert events == ["prepare", "exec:python -m vonk_control.api"]


def test_owned_state_initializes_database_then_the_single_administrator(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vonk_control import api_preexec

    source = tmp_path / "source"
    normalized = tmp_path / "normalized"
    source.mkdir()
    normalized.mkdir()
    (source / "admin-password").write_text("private admin password\n")
    (normalized / "database-url").write_text("postgresql://control/database\n")
    events: list[object] = []
    monkeypatch.setattr(api_preexec.os, "geteuid", lambda: 0)
    monkeypatch.setattr(api_preexec, "_SOURCE_SECRETS", source)
    monkeypatch.setattr(api_preexec, "_NORMALIZED_SECRETS", normalized)
    monkeypatch.setattr(
        api_preexec,
        "stage_compose_secrets",
        lambda source_root, destination_root: events.append(
            ("stage", source_root, destination_root)
        ),
    )
    monkeypatch.setattr(
        api_preexec, "stage_runtime_assets", lambda: events.append("assets")
    )
    monkeypatch.setattr(
        api_preexec, "prepare_shared_volumes", lambda: events.append("volumes")
    )
    monkeypatch.setattr(
        api_preexec,
        "initialize_database",
        lambda database_url: events.append(("database", database_url)),
    )
    monkeypatch.setattr(
        api_preexec,
        "initialize_administrator",
        lambda database_url, password_path: events.append(
            ("administrator", database_url, password_path)
        ),
        raising=False,
    )

    api_preexec.prepare_owned_state()

    assert events == [
        ("stage", source, normalized),
        "assets",
        "volumes",
        ("database", "postgresql://control/database"),
        ("administrator", "postgresql://control/database", source / "admin-password"),
    ]


def test_administrator_initialization_creates_one_login_on_postgres(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    from vonk_control import api_preexec

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
        token_signing_key=b"test-token-signing-key",
        clock=lambda: datetime.now(UTC),
    )
    assert auth.login("admin", password).identity.actor.subject == "admin"
    with pytest.raises(BrowserAuthenticationError):
        auth.login("admin", "different administrator password")


def test_administrator_initialization_rejects_malformed_secret_before_db_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vonk_control import api_preexec

    password_path = tmp_path / "admin-password"
    password_path.write_text("missing final newline", encoding="utf-8")
    monkeypatch.setattr(
        api_preexec,
        "build_engine",
        lambda _database_url: pytest.fail("malformed secret reached the database"),
    )

    with pytest.raises(RuntimeError, match="administrator password secret is invalid"):
        api_preexec.initialize_administrator("postgresql://unused", password_path)
