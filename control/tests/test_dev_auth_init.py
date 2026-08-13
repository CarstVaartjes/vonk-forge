from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.browser_auth import BrowserAuthenticationError, BrowserAuthService
from vonk_control.models import Base, LoginSession, User
from vonk_control.passwords import hash_password, verify_password

OLD_PASSWORD = "synthetic-old-administrator-password"
NEW_PASSWORD = "synthetic-new-administrator-password"
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
OLD_SESSION_TOKEN = "s" * 43


def _module() -> ModuleType:
    return importlib.import_module("vonk_control.dev_auth_init")


def _database(tmp_path: Path) -> tuple[str, sessionmaker[Session]]:
    database = tmp_path / "control.sqlite"
    url = f"sqlite+pysqlite:///{database}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return url, sessionmaker(engine, expire_on_commit=False)


def _auth_root(tmp_path: Path, database_url: str, verifier: str) -> Path:
    root = tmp_path / "auth-secrets"
    root.mkdir(mode=0o700)
    (root / "database-url").write_text(database_url + "\n", encoding="ascii")
    (root / "admin-password-verifier").write_text(verifier + "\n", encoding="ascii")
    for path in root.iterdir():
        path.chmod(0o400)
    root.chmod(0o550)
    return root


def _run(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path,
    mode: str,
) -> int:
    monkeypatch.setenv("VONK_DEV_AUTH_SECRET_ROOT", str(root))
    monkeypatch.setenv("VONK_DEV_AUTH_MODE", mode)
    return int(module.main())


@pytest.mark.parametrize("mode", ["bootstrap", "reconcile"])
def test_dev_auth_init_creates_admin_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    module = _module()
    database_url, sessions = _database(tmp_path)
    verifier = hash_password(NEW_PASSWORD)
    root = _auth_root(tmp_path, database_url, verifier)

    assert _run(module, monkeypatch, root=root, mode=mode) == 0
    first = capsys.readouterr()
    assert first.out == "created\n"
    assert first.err == ""

    assert _run(module, monkeypatch, root=root, mode=mode) == 0
    second = capsys.readouterr()
    assert second.out == "unchanged\n"
    assert second.err == ""
    with sessions() as db:
        users = db.scalars(select(User)).all()
    assert len(users) == 1
    assert users[0].subject == "admin"
    assert users[0].role == "administrator"
    assert users[0].password_verifier == verifier
    combined = first.out + first.err + second.out + second.err
    assert NEW_PASSWORD not in combined
    assert verifier not in combined


def test_dev_auth_init_rejects_an_administrator_role_conflict_without_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module()
    database_url, sessions = _database(tmp_path)
    verifier = hash_password(NEW_PASSWORD)
    root = _auth_root(tmp_path, database_url, verifier)
    with sessions.begin() as db:
        db.add(
            User(
                subject="synthetic-other-user",
                role="administrator",
                password_verifier=hash_password(OLD_PASSWORD),
                disabled_at=None,
            )
        )

    assert _run(module, monkeypatch, root=root, mode="bootstrap") == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "development authentication initialization failed\n"
    assert NEW_PASSWORD not in output.err
    assert verifier not in output.err


@pytest.mark.parametrize("mode", ["rotate", "reconcile"])
def test_dev_auth_init_replaces_changed_verifier_and_revokes_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    module = _module()
    database_url, sessions = _database(tmp_path)
    old_verifier = hash_password(OLD_PASSWORD)
    new_verifier = hash_password(NEW_PASSWORD)
    root = _auth_root(tmp_path, database_url, new_verifier)
    with sessions.begin() as db:
        user = User(
            subject="admin",
            role="administrator",
            password_verifier=old_verifier,
            disabled_at=None,
        )
        db.add(user)
        db.flush()
        db.add(
            LoginSession(
                user_id=user.id,
                digest=sha256(OLD_SESSION_TOKEN.encode("ascii")).hexdigest(),
                expires_at=NOW + timedelta(hours=1),
            )
        )
        db.add(
            User(
                subject="synthetic-operator",
                role="operator",
                password_verifier=None,
                disabled_at=None,
            )
        )

    assert _run(module, monkeypatch, root=root, mode=mode) == 0

    output = capsys.readouterr()
    assert output.out == "rotated\n"
    assert output.err == ""
    with sessions() as db:
        user = db.scalar(select(User).where(User.subject == "admin"))
        login = db.scalar(select(LoginSession))
        operator = db.scalar(
            select(User).where(User.subject == "synthetic-operator")
        )
    assert user is not None
    assert login is not None
    assert operator is not None
    assert operator.role == "operator"
    assert user.password_verifier == new_verifier
    assert login.revoked_at is not None
    assert verify_password(new_verifier, NEW_PASSWORD).valid
    assert not verify_password(new_verifier, OLD_PASSWORD).valid
    service = BrowserAuthService(
        sessions,
        token_signing_key=b"synthetic-browser-auth-signing-key",
        clock=lambda: NOW,
        token_source=lambda: "n" * 43,
    )
    with pytest.raises(BrowserAuthenticationError):
        service.resolve(OLD_SESSION_TOKEN)
    with pytest.raises(BrowserAuthenticationError):
        service.login("admin", OLD_PASSWORD)
    assert service.login("admin", NEW_PASSWORD).identity.actor.subject == "admin"
    assert NEW_PASSWORD not in output.out + output.err
    assert new_verifier not in output.out + output.err


@pytest.mark.parametrize("mode", ["rotate", "reconcile"])
@pytest.mark.parametrize("conflict", ["second-administrator", "admin-wrong-role"])
def test_dev_auth_init_rejects_non_unique_authority_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    conflict: str,
    mode: str,
) -> None:
    module = _module()
    database_url, sessions = _database(tmp_path)
    old_verifier = hash_password(OLD_PASSWORD)
    new_verifier = hash_password(NEW_PASSWORD)
    root = _auth_root(tmp_path, database_url, new_verifier)
    with sessions.begin() as db:
        admin = User(
            subject="admin",
            role="operator" if conflict == "admin-wrong-role" else "administrator",
            password_verifier=old_verifier,
            disabled_at=None,
        )
        db.add(admin)
        db.flush()
        db.add(
            LoginSession(
                user_id=admin.id,
                digest=sha256(b"synthetic-conflicting-authority-session").hexdigest(),
                expires_at=NOW + timedelta(hours=1),
            )
        )
        if conflict == "second-administrator":
            db.add(
                User(
                    subject="synthetic-other-administrator",
                    role="administrator",
                    password_verifier=hash_password("synthetic other password"),
                    disabled_at=None,
                )
            )

    assert _run(module, monkeypatch, root=root, mode=mode) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "development authentication initialization failed\n"
    assert NEW_PASSWORD not in output.err
    assert new_verifier not in output.err
    with sessions() as db:
        persisted_admin = db.scalar(select(User).where(User.subject == "admin"))
        login = db.scalar(select(LoginSession))
    assert persisted_admin is not None
    assert login is not None
    assert persisted_admin.password_verifier == old_verifier
    assert login.revoked_at is None


@pytest.mark.parametrize("mode", ["", "BOOTSTRAP", "unknown"])
def test_dev_auth_init_accepts_only_the_exact_mode_enum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    module = _module()
    database_url, sessions = _database(tmp_path)
    root = _auth_root(tmp_path, database_url, hash_password(NEW_PASSWORD))
    monkeypatch.setenv("VONK_DEV_AUTH_SECRET_ROOT", str(root))
    if mode:
        monkeypatch.setenv("VONK_DEV_AUTH_MODE", mode)
    else:
        monkeypatch.delenv("VONK_DEV_AUTH_MODE", raising=False)

    assert module.main() == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "development authentication initialization failed\n"
    with sessions() as db:
        assert db.scalar(select(User)) is None


@pytest.mark.parametrize("fault", ["mode", "symlink", "hardlink"])
def test_dev_auth_init_rejects_unsafe_secret_files_before_database_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: str,
) -> None:
    module = _module()
    database_url, sessions = _database(tmp_path)
    verifier = hash_password(NEW_PASSWORD)
    root = _auth_root(tmp_path, database_url, verifier)
    target = root / "admin-password-verifier"
    root.chmod(0o700)
    if fault == "mode":
        target.chmod(0o600)
    elif fault == "symlink":
        target.unlink()
        outside = tmp_path / "outside-verifier"
        outside.write_text(verifier + "\n", encoding="ascii")
        outside.chmod(0o400)
        target.symlink_to(outside)
    else:
        os.link(target, tmp_path / "verifier-copy")
    root.chmod(0o550)

    assert _run(module, monkeypatch, root=root, mode="bootstrap") == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "development authentication initialization failed\n"
    assert verifier not in output.err
    with sessions() as db:
        assert db.scalar(select(User)) is None
