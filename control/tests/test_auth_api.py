from __future__ import annotations

import asyncio
import base64
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import cast
from unittest.mock import Mock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.token_support import TokenCodec
from vonk_agent_protocol.error_response import BoundedErrorResponse
from vonk_control.api.application import create_app
from vonk_control.auth import SESSION_COOKIE, Actor
from vonk_control.auth_api import install_auth_routes
from vonk_control.model_primitives import Base
from vonk_control.models.fleet import User
from vonk_control.security.browser_auth import (
    BrowserAuthService,
    BrowserIdentity,
    IssuedCliToken,
    SessionBearerTokens,
)
from vonk_control.security.passwords import hash_password

ADMIN_PASSWORD = "correct horse battery staple"
NOW = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)
ORIGIN = "https://forge.example.test"
SESSION_TOKEN = base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("=")
CSRF_TOKEN = base64.urlsafe_b64encode(b"c" * 32).decode().rstrip("=")
ADMIN_VERIFIER = hash_password(ADMIN_PASSWORD)


class Jobs:
    def enqueue(
        self,
        kind: str,
        actor: str,
        authority_revision: str,
        targets: Sequence[str],
        payload: Mapping[str, object],
        *,
        request_id: str,
    ) -> object:
        raise AssertionError

    def get(self, job_id: str) -> object:
        raise KeyError(job_id)


async def _no_delay() -> None:
    return None


def _client(
    *, failure_delay: Callable[[], Awaitable[None]] = _no_delay
) -> tuple[TestClient, str]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        db.add(
            User(
                subject="admin",
                role="administrator",
                disabled_at=None,
                password_verifier=ADMIN_VERIFIER,
            )
        )
    first = iter((SESSION_TOKEN, CSRF_TOKEN))
    signing_key = b"test-token-signing-key-for-auth-api"
    service = BrowserAuthService(
        sessions,
        clock=lambda: NOW,
        token_source=lambda: next(first, None) or secrets.token_urlsafe(32),
        failure_delay=failure_delay,
    )
    app = create_app(
        jobs=Jobs(),
        tokens=SessionBearerTokens(service, signing_key),
        now=lambda: int(NOW.timestamp()),
        browser_auth=service,
    )
    return TestClient(app, base_url=ORIGIN), ADMIN_VERIFIER


def _login(client: TestClient, password: str = ADMIN_PASSWORD):
    return client.post(
        "/api/auth/login",
        headers={"origin": ORIGIN},
        json={"subject": "admin", "password": password},
    )


def test_auth_openapi_documents_every_runtime_error_status() -> None:
    client, _verifier = _client()

    app = client.app
    assert isinstance(app, FastAPI)
    schema = app.openapi()
    paths = schema["paths"]
    expected = {
        "/api/auth/login": {"401", "403", "422"},
        "/api/auth/session": {"401"},
        "/api/auth/logout": {"401", "403"},
        "/api/auth/cli-token": {"401", "403"},
    }
    for path, statuses in expected.items():
        for method in ("post",) if path != "/api/auth/session" else ("get",):
            responses = paths[path][method]["responses"]
            assert statuses <= set(responses)
            for status_code in statuses - {"422"}:
                response_schema = responses[status_code]["content"]["application/json"][
                    "schema"
                ]
                assert response_schema["$ref"].endswith(BoundedErrorResponse.__name__)


def _chunked_asgi_login(
    app: object,
    *,
    extra_headers: tuple[tuple[bytes, bytes], ...],
) -> tuple[int, int]:
    async def request() -> tuple[int, int]:
        chunks = [
            b'{"subject":"admin","password":"' + b"x" * (600 * 1024),
            b"x" * (500 * 1024),
            b'"}',
        ]
        reads = 0
        sent: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            nonlocal reads
            if reads >= len(chunks):
                return {"type": "http.disconnect"}
            body = chunks[reads]
            reads += 1
            return {
                "type": "http.request",
                "body": body,
                "more_body": reads < len(chunks),
            }

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/auth/login",
            "raw_path": b"/api/auth/login",
            "query_string": b"",
            "headers": (
                (b"content-type", b"application/json"),
                (b"host", b"forge.example.test"),
                (b"origin", b"https://forge.example.test"),
                *extra_headers,
            ),
            "client": ("testclient", 1234),
            "server": ("forge.example.test", 443),
            "root_path": "",
            "state": {},
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=30)  # type: ignore[operator]
        start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        status = start["status"]
        assert isinstance(status, int)
        return status, reads

    return asyncio.run(request())


def test_login_returns_only_a_session_summary_and_exact_secure_cookies() -> None:
    """Dropping bounded cookie flags or exposing credentials must break login."""
    client, verifier = _client()

    response = _login(client)

    assert response.status_code == 200
    assert response.json() == {
        "subject": "admin",
        "role": "administrator",
        "expires_at": "2026-08-13T21:30:00Z",
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers.get_list("set-cookie") == [
        (
            f"__Host-vonk_session={SESSION_TOKEN}; HttpOnly; Max-Age=43200; Path=/; "
            "SameSite=strict; Secure"
        ),
        f"__Host-vonk_csrf={CSRF_TOKEN}; Max-Age=43200; Path=/; SameSite=strict; Secure",
    ]
    serialized = repr((response.headers.items(), response.content))
    assert ADMIN_PASSWORD not in serialized
    assert verifier not in serialized


@pytest.mark.parametrize(
    "document",
    [
        {"subject": "Admin", "password": ADMIN_PASSWORD},
        {"subject": 7, "password": ADMIN_PASSWORD},
        {"subject": "admin", "password": ""},
        {"subject": "admin", "password": "x" * 257},
        {"subject": "admin", "password": "é" * 129},
        {"subject": "admin", "password": 7},
        {"subject": "admin", "password": ADMIN_PASSWORD, "redirect": "/fleet"},
    ],
)
def test_login_rejects_non_exact_or_unbounded_strict_documents(
    document: dict[str, object],
) -> None:
    """Coercion, extra fields, or out-of-bound credentials must fail pre-auth."""
    client, _verifier = _client()

    response = client.post("/api/auth/login", headers={"origin": ORIGIN}, json=document)

    assert response.status_code == 422
    assert response.json() == {"detail": "login request is invalid"}
    password = document.get("password")
    if isinstance(password, str) and password:
        assert password not in response.text


@pytest.mark.parametrize(
    "document",
    [
        '{"subject":"admin","subject":"admin","password":"duplicate-secret"}',
        '{"subject":"admin","password":"malformed-secret"',
    ],
)
def test_login_rejects_duplicate_or_malformed_json_without_echoing_it(
    document: str,
) -> None:
    """Invalid JSON structure must fail before auth without reflecting secrets."""
    client, _verifier = _client()

    response = client.post(
        "/api/auth/login",
        headers={"origin": ORIGIN, "content-type": "application/json"},
        content=document,
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "login request is invalid"}
    assert "secret" not in response.text


@pytest.mark.parametrize(
    "headers",
    [
        ((b"transfer-encoding", b"chunked"),),
        ((b"content-length", b"64"),),
    ],
)
def test_login_stops_reading_asgi_chunks_at_the_actual_request_limit(
    headers: tuple[tuple[bytes, bytes], ...],
) -> None:
    """Missing or false length metadata must not let login buffer an oversized body."""
    client, _verifier = _client()

    status, reads = _chunked_asgi_login(client.app, extra_headers=headers)

    assert status == 413
    assert reads == 2


@pytest.mark.parametrize(
    "origin",
    [None, "http://forge.example.test", "https://other.example.test"],
)
def test_login_requires_the_exact_https_request_origin(origin: str | None) -> None:
    """Missing, non-HTTPS, or cross-origin login requests must fail closed."""
    client, _verifier = _client()
    headers = {} if origin is None else {"origin": origin}

    response = client.post(
        "/api/auth/login",
        headers=headers,
        json={"subject": "admin", "password": ADMIN_PASSWORD},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "origin validation failed"}


def test_login_uses_generic_credential_failure() -> None:
    """Credential failures must reveal neither cause nor submitted material."""
    client, verifier = _client()

    response = _login(client, "wrong password")

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication failed"}
    serialized = repr((response.headers.items(), response.content))
    assert "wrong password" not in serialized
    assert verifier not in serialized


def test_failed_logins_never_lock_out_the_correct_password() -> None:
    """A failure count that answers 429 would let any peer lock the owner out."""
    delays: list[None] = []

    async def delay() -> None:
        delays.append(None)

    client, _verifier = _client(failure_delay=delay)

    assert [_login(client, f"wrong {index}").status_code for index in range(8)] == [
        401
    ] * 8
    assert len(delays) == 8
    assert _login(client).status_code == 200
    assert len(delays) == 8


def test_session_status_and_logout_use_the_durable_cookie_session() -> None:
    """Status must summarize the session and logout must revoke it without a body."""
    client, _verifier = _client()
    assert _login(client).status_code == 200

    status = client.get("/api/auth/session")

    assert status.status_code == 200
    assert status.json() == {
        "subject": "admin",
        "role": "administrator",
        "expires_at": "2026-08-13T21:30:00Z",
    }
    assert status.headers["cache-control"] == "no-store"

    bearer = _mint(client)
    bypass = client.post(
        "/api/auth/logout",
        headers={"authorization": f"Bearer {bearer}"},
    )
    assert bypass.status_code == 403
    assert client.get("/api/auth/session").status_code == 200

    logout = client.post(
        "/api/auth/logout",
        headers={
            "authorization": f"Bearer {bearer}",
            "x-csrf-token": CSRF_TOKEN,
        },
    )

    assert logout.status_code == 204
    assert logout.content == b""
    assert logout.headers["cache-control"] == "no-store"
    cleared = logout.headers.get_list("set-cookie")
    assert len(cleared) == 2
    assert cleared[0].startswith('__Host-vonk_session="";')
    assert "HttpOnly" in cleared[0]
    assert "Max-Age=0" in cleared[0]
    assert cleared[1].startswith('__Host-vonk_csrf="";')
    assert "HttpOnly" not in cleared[1]
    assert "Max-Age=0" in cleared[1]
    assert client.get("/api/auth/session").status_code == 401


def test_cli_token_is_a_browser_session_only_download_with_expiry() -> None:
    """The account menu can mint a file without exposing a token in JSON or URLs."""
    client, _verifier = _client()
    assert _login(client).status_code == 200

    response = client.post(
        "/api/auth/cli-token",
        headers={"x-csrf-token": CSRF_TOKEN},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="vonkctl-token"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-vonk-token-expires-at"] == "2026-09-12T09:30:00Z"
    token = response.text.strip()
    listed = client.get(
        "/api/auth/cli-tokens", headers={"authorization": f"Bearer {token}"}
    )
    assert listed.status_code == 200
    [summary] = listed.json()["tokens"]
    assert summary["presented"] is True
    assert summary["revoked"] is False
    assert token not in listed.text


def _mint(client: TestClient) -> str:
    if SESSION_COOKIE not in client.cookies:
        assert _login(client).status_code == 200
    response = client.post("/api/auth/cli-token", headers={"x-csrf-token": CSRF_TOKEN})
    assert response.status_code == 200
    return response.text.strip()


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def test_revoking_one_cli_token_leaves_the_others_working() -> None:
    # Break caught: revocation that is not looked up per request (a stateless
    # bearer) keeps a stolen laptop's token valid for its whole 30 days.
    client, _verifier = _client()
    stolen, kept = _mint(client), _mint(client)
    listed = client.get("/api/auth/cli-tokens", headers=_bearer(kept)).json()["tokens"]
    stolen_id = next(item["id"] for item in listed if not item["presented"])

    revoked = client.post(
        f"/api/auth/cli-tokens/{stolen_id}/revoke", headers=_bearer(kept)
    )

    assert revoked.status_code == 204
    assert (
        client.get("/api/auth/cli-tokens", headers=_bearer(stolen)).status_code == 401
    )
    after = client.get("/api/auth/cli-tokens", headers=_bearer(kept))
    assert after.status_code == 200
    flags = {item["id"]: item["revoked"] for item in after.json()["tokens"]}
    assert flags[stolen_id]
    unknown = "00000000-0000-4000-8000-000000000000"
    assert (
        client.post(
            f"/api/auth/cli-tokens/{unknown}/revoke", headers=_bearer(kept)
        ).status_code
        == 404
    )


def test_revoke_all_ends_every_cli_token_and_browser_session() -> None:
    client, _verifier = _client()
    first, second = _mint(client), _mint(client)

    response = client.post("/api/auth/cli-tokens/revoke-all", headers=_bearer(first))

    assert response.status_code == 204
    for token in (first, second):
        assert (
            client.get("/api/auth/cli-tokens", headers=_bearer(token)).status_code
            == 401
        )
    assert client.get("/api/auth/session").status_code == 401


def test_token_kinds_never_cross_and_stateless_tokens_are_refused() -> None:
    # Break caught: a resolver that ignores the row kind lets a CLI token act
    # as a browser cookie (and mint more tokens), or a session cookie value act
    # as a 30-day bearer; a leftover signature check accepts pre-row tokens.
    client, _verifier = _client()
    cli_token = _mint(client)
    stateless = TokenCodec(b"test-token-signing-key-for-auth-api").issue(
        Actor("admin", "administrator"), ttl_seconds=60, now=int(NOW.timestamp())
    )

    assert (
        client.get("/api/auth/cli-tokens", headers=_bearer(SESSION_TOKEN)).status_code
        == 401
    )
    assert (
        client.get("/api/auth/cli-tokens", headers=_bearer(stateless)).status_code
        == 401
    )
    cookie_only = TestClient(client.app, base_url=ORIGIN)
    cookie_only.cookies.set(SESSION_COOKIE, cli_token)
    assert cookie_only.get("/api/auth/session").status_code == 401


def test_cli_token_cannot_be_minted_with_a_bearer_or_without_csrf() -> None:
    client, _verifier = _client()
    bearer = _mint(client)
    client.cookies.clear()
    assert (
        client.post(
            "/api/auth/cli-token",
            headers={"authorization": f"Bearer {bearer}"},
        ).status_code
        == 401
    )
    assert _login(client).status_code == 200
    assert client.post("/api/auth/cli-token").status_code == 403


def test_cli_token_checks_csrf_even_when_actor_dependency_already_authenticated():
    # Break caught: moving session authentication into another dependency can
    # accidentally remove the token-minting route's explicit CSRF check.

    service = Mock()
    service.resolve.return_value = BrowserIdentity(
        Actor("admin", "administrator"), NOW, "session"
    )
    service.issue_cli_token.return_value = IssuedCliToken("cli", NOW, "token")
    app = FastAPI()
    install_auth_routes(
        app,
        cast(BrowserAuthService, service),
        Depends(lambda: Actor("admin", "administrator")),
    )
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set("__Host-vonk_session", SESSION_TOKEN)
    client.cookies.set("__Host-vonk_csrf", CSRF_TOKEN)
    assert client.post("/api/auth/cli-token").status_code == 403
    assert (
        client.post(
            "/api/auth/cli-token", headers={"x-csrf-token": "wrong"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/auth/cli-token", headers={"x-csrf-token": CSRF_TOKEN}
        ).status_code
        == 200
    )


def test_login_without_host_is_refused_before_password_verification():
    # Break caught: direct Host indexing raises KeyError instead of refusing
    # the origin check for an HTTP request with no Host header.
    client, _ = _client()
    app = client.app

    async def without_host(scope, receive, send):
        scope["headers"] = [
            (name, value) for name, value in scope["headers"] if name != b"host"
        ]
        await app(scope, receive, send)

    assert _login(TestClient(without_host, base_url=ORIGIN)).status_code == 403
