from __future__ import annotations

from pathlib import Path
from typing import Self

import pytest
from vonk_control import dev_litellm_database
from vonk_control.dev_litellm_database import (
    DevLiteLLMDatabaseError,
    _inputs,
    _reconcile,
)


class FakeCursor:
    def __init__(self, rows: list[tuple[str, ...] | None]) -> None:
        self._rows = iter(rows)
        self.executed: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: object, parameters: object = None) -> None:
        rendered = query.as_string() if hasattr(query, "as_string") else str(query)
        self.executed.append(
            rendered if parameters is None else f"{rendered} {parameters!r}"
        )

    def fetchone(self) -> tuple[str, ...] | None:
        return next(self._rows)


class FakeConnection:
    def __init__(self, rows: list[tuple[str, ...] | None]) -> None:
        self.cursor_instance = FakeCursor(rows)

    def cursor(self) -> FakeCursor:
        return self.cursor_instance


def _secret_root(tmp_path: Path) -> Path:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    values = {
        "database-url": "postgresql+psycopg://control:admin-secret@postgres:5432/control\n",
        "litellm-database-password": "a" * 64 + "\n",
    }
    for name, value in values.items():
        path = root / name
        path.write_text(value, encoding="ascii")
        path.chmod(0o400)
    root.chmod(0o550)
    return root


def test_inputs_accept_exact_projected_secrets(tmp_path: Path) -> None:
    settings, password = _inputs(_secret_root(tmp_path))

    assert settings == {
        "host": "postgres",
        "port": 5432,
        "dbname": "control",
        "user": "control",
        "password": "admin-secret",
    }
    assert password == "a" * 64


@pytest.mark.parametrize(
    ("name", "value", "message"),
    (
        ("litellm-database-password", "not-hex\n", "password is invalid"),
        (
            "database-url",
            "sqlite+pysqlite:///control.sqlite\n",
            "database URL is invalid",
        ),
        (
            "database-url",
            "postgresql://control:secret@elsewhere/control\n",
            "database URL is invalid",
        ),
    ),
)
def test_inputs_reject_invalid_authority(
    tmp_path: Path, name: str, value: str, message: str
) -> None:
    root = _secret_root(tmp_path)
    root.chmod(0o700)
    (root / name).chmod(0o600)
    (root / name).write_text(value, encoding="ascii")
    (root / name).chmod(0o400)
    root.chmod(0o550)

    with pytest.raises(DevLiteLLMDatabaseError, match=message):
        _inputs(root)


def test_inputs_reject_symlinked_secret(tmp_path: Path) -> None:
    root = _secret_root(tmp_path)
    root.chmod(0o700)
    target = root / "litellm-database-password"
    target.unlink()
    outside = tmp_path / "outside"
    outside.write_text("b" * 64 + "\n", encoding="ascii")
    outside.chmod(0o400)
    target.symlink_to(outside)
    root.chmod(0o550)

    with pytest.raises(DevLiteLLMDatabaseError, match="secret is unsafe"):
        _inputs(root)


def test_reconcile_creates_missing_role_and_database() -> None:
    connection = FakeConnection([None, None, ("litellm",)])

    _reconcile(connection, "a" * 64)

    statements = connection.cursor_instance.executed
    assert any('CREATE ROLE "litellm" LOGIN PASSWORD' in item for item in statements)
    assert any('CREATE DATABASE "litellm" OWNER "litellm"' in item for item in statements)


def test_reconcile_rotates_role_password_idempotently() -> None:
    connection = FakeConnection([("litellm",), ("litellm",), ("litellm",)])

    _reconcile(connection, "b" * 64)

    statements = connection.cursor_instance.executed
    assert any('ALTER ROLE "litellm" LOGIN PASSWORD' in item for item in statements)
    assert not any("CREATE DATABASE" in item for item in statements)


def test_reconcile_rejects_database_owned_by_another_role() -> None:
    connection = FakeConnection([("postgres",)])

    with pytest.raises(DevLiteLLMDatabaseError, match="owner is invalid"):
        _reconcile(connection, "c" * 64)

    assert not any(
        action in item
        for item in connection.cursor_instance.executed
        for action in ("CREATE ROLE", "ALTER ROLE", "CREATE DATABASE")
    )


def test_main_reports_only_a_generic_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        dev_litellm_database,
        "_run",
        lambda: (_ for _ in ()).throw(DevLiteLLMDatabaseError("secret-value")),
    )

    assert dev_litellm_database.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "development LiteLLM database initialization failed\n"
    assert "secret-value" not in captured.err
