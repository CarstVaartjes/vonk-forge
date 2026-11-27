"""The stored-login expiry warning and the login files' modes (fake clock)."""

from __future__ import annotations

import io
import json
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.message import Message
from pathlib import Path
from typing import Self

import pytest

from cluster_profiles import cli_auth
from cluster_profiles.control_client import client as client_module
from cluster_profiles.control_client.client import ControlClient
from cluster_profiles.control_client.connection import (
    StoredConnection,
    connection_path,
    days_until,
    expiry_warning,
    login_token_path,
    resolve_credential,
    write_private,
)
from cluster_profiles.control_client.errors import (
    ControlCredentialScope,
    ControlUnauthorized,
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
    assert credential.from_login
    assert credential.expires_at == _NOW + timedelta(days=30)
    assert stat.S_IMODE(login_token_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(login_token_path().parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("days_left", "expected"),
    [(30, None), (8, None), (6, "expires in 6 days"), (-2, "expired on")],
)
def test_warning_starts_seven_days_before_expiry(
    login, days_left: int, expected: str | None
) -> None:
    login(_NOW + timedelta(days=days_left))

    warning = expiry_warning(resolve_credential(), _NOW)

    if expected is None:
        assert warning is None
    else:
        assert warning is not None
        assert expected in warning
        assert "vonkctl auth login" in warning


def test_an_expired_stored_login_warns_once_and_still_connects(
    login, capsys: pytest.CaptureFixture[str]
) -> None:
    login(_NOW - timedelta(days=1))

    client = [ControlClient.from_environment(now=lambda: _NOW) for _ in range(2)][-1]

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

    assert credential.token_file == token
    assert credential.expires_at is None
    assert expiry_warning(credential, _NOW) is None


def test_the_login_token_is_not_sent_to_another_controller(
    login, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(_NOW + timedelta(days=30))
    monkeypatch.setenv("VONK_CONTROL_URL", "https://other.example.test")

    with pytest.raises(ControlCredentialScope):
        resolve_credential()


def test_a_login_without_a_readable_connection_is_not_sent_to_an_env_url(
    login, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(_NOW + timedelta(days=30))
    monkeypatch.setenv("VONK_CONTROL_URL", "https://forge.example.test")
    for damage in (
        lambda: connection_path().write_text("{not json"),
        connection_path().unlink,
    ):
        damage()
        with pytest.raises(ControlCredentialScope):
            resolve_credential()
    assert cli_auth.read_connection_or_none() is None


def test_a_fresh_token_reads_thirty_days() -> None:
    assert days_until(_NOW + timedelta(days=30), _NOW + timedelta(seconds=2)) == 30


@pytest.mark.parametrize("raw", [b"\xff\xfesecret\n"])
def test_a_bad_stdin_password_fails_without_echoing_its_bytes(
    monkeypatch: pytest.MonkeyPatch, raw: bytes
) -> None:
    monkeypatch.setattr(
        sys, "stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    )

    with pytest.raises(cli_auth.LoginFailed) as failure:
        cli_auth._read_password(from_stdin=True)

    assert "secret" not in str(failure.value)
    assert "\xff" not in str(failure.value)


_THIS_LOGIN = "22222222-2222-4222-8222-222222222222"


class _Answer:
    def __init__(self, status: int, payload: object | None = None) -> None:
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self._body = b"" if payload is None else json.dumps(payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        return self._body[:maximum]


def _controller(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[str, str], _Answer]
) -> list[tuple[str, str]]:
    """Fake the Controller at the HTTPS boundary every vonkctl request uses."""
    calls: list[tuple[str, str]] = []

    def opener(request: urllib.request.Request, *, timeout: float) -> _Answer:
        del timeout
        method = request.get_method()
        path = urllib.parse.urlsplit(request.full_url).path
        calls.append((method, path))
        return answer(method, path)

    monkeypatch.setattr(client_module, "open_https", opener)
    return calls


def _listing(_method: str, _path: str) -> _Answer:
    if _method == "POST":
        return _Answer(204)
    return _Answer(
        200,
        {
            "tokens": [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "expires_at": "2026-12-21T12:00:00Z",
                    "revoked": False,
                    "presented": False,
                },
                {
                    "id": _THIS_LOGIN,
                    "expires_at": "2026-12-21T12:00:00Z",
                    "revoked": False,
                    "presented": True,
                },
            ]
        },
    )


def _denied(_method: str, path: str) -> _Answer:
    raise urllib.error.HTTPError(path, 401, "denied", Message(), io.BytesIO(b"{}"))


def test_logout_revokes_only_this_logins_token_then_deletes_the_files(
    login, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Break caught: a logout that only deletes files leaves a copied token
    # file valid for 30 days; one that revokes the wrong row logs out another
    # machine.
    login(_NOW + timedelta(days=30))
    calls = _controller(monkeypatch, _listing)

    result = cli_auth.run_auth("logout", url=None, password_stdin=False)

    assert result.document() == {"token_revocation": "revoked"}
    assert calls == [
        ("GET", "/api/auth/cli-tokens"),
        ("POST", f"/api/auth/cli-tokens/{_THIS_LOGIN}/revoke"),
    ]
    assert not login_token_path().exists()
    assert not connection_path().exists()


@pytest.mark.parametrize(
    ("answer", "expected"),
    [(_denied, "not-accepted"), (None, "unconfirmed")],
)
def test_logout_without_a_revocation_still_removes_the_local_login(
    login,
    monkeypatch: pytest.MonkeyPatch,
    answer: Callable[[str, str], _Answer] | None,
    expected: str,
) -> None:
    # Break caught: a failed revocation reported like a refused token, or a
    # logout that keeps the files when revocation fails.
    login(_NOW + timedelta(days=30))

    def revocation_fails(method: str, path: str) -> _Answer:
        if method == "GET":
            return _listing(method, path)
        raise urllib.error.HTTPError(path, 500, "failed", Message(), io.BytesIO(b"{}"))

    _controller(monkeypatch, answer or revocation_fails)

    result = cli_auth.run_auth("logout", url=None, password_stdin=False)

    assert result.document() == {"token_revocation": expected}
    assert not login_token_path().exists()


def test_a_refused_token_listing_is_the_standard_unauthorized_error(
    login, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(_NOW + timedelta(days=30))
    _controller(monkeypatch, _denied)

    with pytest.raises(ControlUnauthorized):
        cli_auth.run_auth("tokens", url=None, password_stdin=False)


@pytest.mark.parametrize("prefix", ["__Host-", ""])
def test_login_replays_the_session_cookies_under_the_names_the_controller_set(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    """Matching one exact cookie name would strand vonkctl on the other Controller."""
    sent: dict[str, dict[str, str]] = {}

    def controller(
        _url: str, path: str, headers: dict[str, str], _body: bytes | None = None
    ) -> tuple[int, bytes, Message]:
        sent[path] = headers
        answer = Message()
        if path == "/api/auth/login":
            answer["set-cookie"] = (
                f"{prefix}vonk_session=s3ss; HttpOnly; Path=/; Secure"
            )
            answer["set-cookie"] = f"{prefix}vonk_csrf=csrf; Path=/; Secure"
            return 200, b"{}", answer
        answer["x-vonk-token-expires-at"] = "2026-12-21T12:00:00Z"
        return 200, b"minted-token", answer

    monkeypatch.setattr(cli_auth, "_post", controller)

    token, _expires = cli_auth._mint_token("https://forge.example.test", "pw")

    assert token == "minted-token"
    minting = sent["/api/auth/cli-token"]
    assert minting["Cookie"] == f"{prefix}vonk_session=s3ss; {prefix}vonk_csrf=csrf"
    assert minting["X-CSRF-Token"] == "csrf"
