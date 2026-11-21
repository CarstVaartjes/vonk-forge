"""The stored-login expiry warning and the login files' modes (fake clock)."""

from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cluster_profiles.control_client import client as client_module
from cluster_profiles.control_client.client import ControlClient
from cluster_profiles.control_client.connection import (
    StoredConnection,
    connection_path,
    expiry_warning,
    login_token_path,
    resolve_credential,
    write_private,
)

_NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("VONK_CONTROL_URL", raising=False)
    monkeypatch.delenv("VONK_CONTROL_TOKEN_FILE", raising=False)
    monkeypatch.setattr(client_module, "_WARNED", set())

    def store(expires_at: datetime) -> None:
        write_private(login_token_path(), b"fixture-token\n")
        write_private(
            connection_path(),
            StoredConnection("https://forge.example.test", expires_at).document(),
        )

    return store


def test_login_files_are_private_and_resolve_without_env(login) -> None:
    login(_NOW + timedelta(days=30))

    credential = resolve_credential()

    assert credential.url == "https://forge.example.test"
    assert credential.from_login and credential.expires_at == _NOW + timedelta(days=30)
    assert stat.S_IMODE(login_token_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(login_token_path().parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("days_left", "expected"),
    [(30, None), (8, None), (6, "expires in 6 days"), (-2, "expired on")],
)
def test_warning_starts_seven_days_before_expiry(
    login, days_left: int, expected: str | None
) -> None:
    login(_NOW + timedelta(days=days_left, minutes=1))

    warning = expiry_warning(resolve_credential(), _NOW)

    if expected is None:
        assert warning is None
    else:
        assert warning is not None
        assert expected in warning and "vonkctl auth login" in warning


def test_an_expired_stored_login_warns_once_and_still_connects(
    login, capsys: pytest.CaptureFixture[str]
) -> None:
    login(_NOW - timedelta(days=1))

    for _ in range(2):
        client = ControlClient.from_environment(now=lambda: _NOW)

    assert isinstance(client, ControlClient)
    assert capsys.readouterr().err.count("vonkctl auth login") == 1


def test_an_env_token_file_has_unknown_expiry_and_no_warning(
    login, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    login(_NOW - timedelta(days=1))
    token = tmp_path / "env-token"
    token.write_text("env-token\n")
    token.chmod(0o600)
    monkeypatch.setenv("VONK_CONTROL_TOKEN_FILE", str(token))

    credential = resolve_credential()

    assert credential.token_file == token and credential.expires_at is None
    assert expiry_warning(credential, _NOW) is None
