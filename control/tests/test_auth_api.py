from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.browser_auth import BrowserAuthService, LoginRateLimiter
from vonk_control.models import Base, User
from vonk_control.passwords import hash_password

ADMIN_PASSWORD = "correct horse battery staple"
NOW = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)
ORIGIN = "https://forge.example.test"
SESSION_TOKEN = base64.urlsafe_b64encode(b"s" * 32).decode().rstrip("=")
CSRF_TOKEN = base64.urlsafe_b64encode(b"c" * 32).decode().rstrip("=")
ADMIN_VERIFIER = hash_password(ADMIN_PASSWORD)


class Jobs:
    def get(self, job_id: str) -> object:
        raise KeyError(job_id)


def _client(
    *, maximum_subject_failures: int = 5
) -> tuple[TestClient, MemoryAuditStore, str]:
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
    tokens = iter((SESSION_TOKEN, CSRF_TOKEN))
    signing_key = b"test-token-signing-key-for-auth-api"
    service = BrowserAuthService(
        sessions,
        token_signing_key=signing_key,
        clock=lambda: NOW,
        token_source=lambda: next(tokens),
        rate_limiter=LoginRateLimiter(
            token_signing_key=signing_key,
            maximum_subject_failures=maximum_subject_failures,
        ),
    )
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(),
        tokens=TokenCodec(signing_key),
        audits=audits,
        fleet=lambda: {"nodes": []},
        now=lambda: int(NOW.timestamp()),
        browser_auth=service,
    )
    return TestClient(app, base_url=ORIGIN), audits, ADMIN_VERIFIER


def _login(client: TestClient, password: str = ADMIN_PASSWORD):
    return client.post(
        "/api/v1/auth/login",
        headers={"origin": ORIGIN},
        json={"subject": "admin", "password": password},
    )


def test_login_returns_only_a_session_summary_and_exact_secure_cookies() -> None:
    """Dropping bounded cookie flags or exposing credentials must break login."""
    client, audits, verifier = _client()

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
            f"vonk_session={SESSION_TOKEN}; HttpOnly; Max-Age=43200; Path=/; "
            "SameSite=strict; Secure"
        ),
        f"vonk_csrf={CSRF_TOKEN}; Max-Age=43200; Path=/; SameSite=strict; Secure",
    ]
    serialized = repr((response.headers.items(), response.content))
    bearer = TokenCodec(b"test-token-signing-key-for-auth-api").issue(
        Actor("admin", "administrator"),
        ttl_seconds=60,
        now=0,
    )
    assert ADMIN_PASSWORD not in serialized
    assert verifier not in serialized
    assert bearer not in serialized
    event = audits.for_request(response.headers["x-request-id"])
    assert (event.actor, event.action, event.base_commit, event.targets) == (
        "admin",
        "auth.login.succeeded",
        None,
        (),
    )


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
    client, audits, _verifier = _client()

    response = client.post(
        "/api/v1/auth/login", headers={"origin": ORIGIN}, json=document
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "login request is invalid"}
    password = document.get("password")
    if isinstance(password, str) and password:
        assert password not in response.text
    assert audits.list() == []


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
    client, audits, _verifier = _client()

    response = client.post(
        "/api/v1/auth/login",
        headers={"origin": ORIGIN, "content-type": "application/json"},
        content=document,
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "login request is invalid"}
    assert "secret" not in response.text
    assert audits.list() == []


@pytest.mark.parametrize(
    "origin",
    [None, "http://forge.example.test", "https://other.example.test"],
)
def test_login_requires_the_exact_https_request_origin(origin: str | None) -> None:
    """Missing, non-HTTPS, or cross-origin login requests must fail closed."""
    client, audits, _verifier = _client()
    headers = {} if origin is None else {"origin": origin}

    response = client.post(
        "/api/v1/auth/login",
        headers=headers,
        json={"subject": "admin", "password": ADMIN_PASSWORD},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "origin validation failed"}
    assert audits.list() == []


def test_login_uses_generic_credential_failure_and_bounded_audit() -> None:
    """Credential failures must reveal neither cause nor submitted material."""
    client, audits, verifier = _client()

    response = _login(client, "wrong password")

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication failed"}
    serialized = repr((response.headers.items(), response.content, audits.list()))
    assert "wrong password" not in serialized
    assert verifier not in serialized
    event = audits.for_request(response.headers["x-request-id"])
    assert (event.actor, event.action, event.base_commit, event.targets) == (
        "anonymous",
        "auth.login.failed",
        None,
        (),
    )


def test_login_uses_generic_throttle_response_and_bounded_audit() -> None:
    """Rate limiting must be a generic 429 with no credential disclosure."""
    client, audits, _verifier = _client(maximum_subject_failures=1)
    assert _login(client, "first wrong password").status_code == 401

    response = _login(client, "second wrong password")

    assert response.status_code == 429
    assert response.json() == {"detail": "authentication temporarily unavailable"}
    assert "second wrong password" not in repr(
        (response.headers.items(), response.content, audits.list())
    )
    event = audits.for_request(response.headers["x-request-id"])
    assert (event.actor, event.action, event.base_commit, event.targets) == (
        "anonymous",
        "auth.login.throttled",
        None,
        (),
    )


def test_session_status_and_logout_use_the_durable_cookie_session() -> None:
    """Status must summarize the session and logout must revoke it without a body."""
    client, audits, _verifier = _client()
    assert _login(client).status_code == 200

    status = client.get("/api/v1/auth/session")

    assert status.status_code == 200
    assert status.json() == {
        "subject": "admin",
        "role": "administrator",
        "expires_at": "2026-08-13T21:30:00Z",
    }
    assert status.headers["cache-control"] == "no-store"

    bearer = TokenCodec(b"test-token-signing-key-for-auth-api").issue(
        Actor("bearer-operator", "operator"),
        ttl_seconds=60,
        now=int(NOW.timestamp()),
    )
    bypass = client.post(
        "/api/v1/auth/logout",
        headers={"authorization": f"Bearer {bearer}"},
    )
    assert bypass.status_code == 403
    assert client.get("/api/v1/auth/session").status_code == 200

    logout = client.post(
        "/api/v1/auth/logout",
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
    assert cleared[0].startswith('vonk_session="";')
    assert "HttpOnly" in cleared[0]
    assert "Max-Age=0" in cleared[0]
    assert cleared[1].startswith('vonk_csrf="";')
    assert "HttpOnly" not in cleared[1]
    assert "Max-Age=0" in cleared[1]
    event = audits.for_request(logout.headers["x-request-id"])
    assert (event.actor, event.action, event.base_commit, event.targets) == (
        "admin",
        "auth.logout",
        None,
        (),
    )
    assert client.get("/api/v1/auth/session").status_code == 401
