import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.api import create_app, create_preselection_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.browser_auth import BrowserAuthService
from vonk_control.models import Base, User
from vonk_control.passwords import hash_password

ADMIN_PASSWORD = "correct horse battery staple"
ADMIN_VERIFIER = hash_password(ADMIN_PASSWORD)


@dataclass
class Enqueued:
    id: str = "job-1"
    state: str = "queued"


class Jobs:
    def __init__(self) -> None:
        self.calls = []

    def enqueue(self, kind, actor, authority_revision, targets, payload, *, request_id):
        self.calls.append((kind, actor, authority_revision, targets, payload, request_id))
        return Enqueued()

    def get(self, job_id):
        return Enqueued(id=job_id)


def _client(role: str, *, generic_jobs_enabled: bool = True):
    codec = TokenCodec(b"k" * 32)
    audits = MemoryAuditStore()
    jobs = Jobs()
    app = create_app(
        jobs=jobs,
        tokens=codec,
        audits=audits,
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
        generic_jobs_enabled=generic_jobs_enabled,
    )
    client = TestClient(app)
    token = codec.issue(Actor(role, role), ttl_seconds=1000, now=0)
    return client, {"Authorization": f"Bearer {token}"}, jobs, audits


@dataclass
class Clock:
    value: datetime = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _opaque(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode().rstrip("=")


def _browser_client():
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
    clock = Clock()
    tokens = iter((_opaque(1), _opaque(2)))
    service = BrowserAuthService(
        sessions,
        token_signing_key=b"k" * 32,
        clock=clock,
        token_source=lambda: next(tokens),
    )
    issued = service.login("admin", ADMIN_PASSWORD)
    codec = TokenCodec(b"k" * 32)
    jobs = Jobs()
    app = create_app(
        jobs=jobs,
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
        generic_jobs_enabled=True,
        browser_auth=service,
    )
    client = TestClient(app, base_url="https://forge.example.test")
    return client, issued, service, sessions, clock, codec, jobs


def test_health_is_public_but_fleet_requires_authentication() -> None:
    client, _, _, _ = _client("viewer")
    assert client.get("/api/v1/healthz").status_code == 200
    assert client.get("/api/v1/fleet").status_code == 401


def test_request_boundary_admits_large_recipe_images_only_on_exact_put_route() -> None:
    client, _, _, _ = _client("viewer")
    build_id = "00000000-0000-4000-8000-000000000001"
    route = f"/agent/v1/recipe-builds/{build_id}/image"
    body = b"x" * 1_048_577

    # The missing trusted-proxy identity is rejected after the request-size
    # boundary, proving that the protocol's streaming image route was admitted.
    assert client.put(route, content=body).status_code == 401

    # Method and path are both part of the authority boundary. Near-matches
    # retain the ordinary one-MiB API ceiling.
    assert client.post(route, content=body).status_code == 413
    assert client.put(f"{route}/extra", content=body).status_code == 413


def test_preselection_is_a_distinct_app_factory() -> None:
    assert create_preselection_app is not create_app


def test_removed_package_and_deployment_routes_are_not_registered() -> None:
    client, _, _, _ = _client("administrator")
    package_prefix = "/api/v1/" + "packages/"
    deployment_prefix = "/api/v1/" + "deployments"
    legacy_paths = {
        route.path
        for route in client.app.routes
        if route.path.startswith(package_prefix)
        or route.path.startswith(deployment_prefix)
    }
    assert legacy_paths == set()


def test_viewer_cannot_enqueue_mutation() -> None:
    client, headers, _, _ = _client("viewer")
    response = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "kind": "probe",
            "authority_revision": "abc",
            "targets": ["node"],
            "payload": {},
        },
    )
    assert response.status_code == 403


def test_admin_mutation_is_correlated_and_audited() -> None:
    client, headers, jobs, audits = _client("administrator")
    response = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "kind": "probe",
            "authority_revision": "abc",
            "targets": ["node"],
            "payload": {"safe": True},
        },
    )
    assert response.status_code == 202
    request_id = response.headers["x-request-id"]
    assert jobs.calls[0][1:4] == ("administrator", "abc", ["node"])
    event = audits.for_request(request_id)
    assert (event.actor, event.authority_revision, event.targets) == (
        "administrator",
        "abc",
        ("node",),
    )


def test_generic_job_endpoint_cannot_create_reconciliation_authority() -> None:
    client, headers, jobs, _audits = _client("administrator")

    response = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "kind": "reconcile",
            "authority_revision": "a"  * 64,
            "targets": ["spk_" + "1" * 32],
            "payload": {"reconciliation_id": "attacker-controlled"},
        },
    )

    assert response.status_code == 422
    assert jobs.calls == []


def test_production_boundary_rejects_direct_probe_job_submission() -> None:
    client, headers, jobs, _audits = _client(
        "administrator",
        generic_jobs_enabled=False,
    )

    response = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "kind": "probe",
            "authority_revision": "a"  * 64,
            "targets": ["spk_" + "1" * 32],
            "payload": {},
        },
    )

    assert response.status_code == 422
    assert jobs.calls == []


def test_cookie_authentication_resolves_only_through_browser_sessions() -> None:
    """Opaque browser tokens must work without being signed bearer tokens."""
    client, issued, _service, _sessions, _clock, _codec, _jobs = _browser_client()
    client.cookies.set("vonk_session", issued.token)

    response = client.get("/api/v1/audit")

    assert response.status_code == 200


def test_cookie_authenticated_mutation_requires_matching_csrf() -> None:
    client, issued, _service, _sessions, _clock, _codec, jobs = _browser_client()
    client.cookies.set("vonk_session", issued.token)
    document = {
        "kind": "probe",
        "authority_revision": "abc",
        "targets": [],
        "payload": {},
    }

    assert client.post("/api/v1/jobs", json=document).status_code == 403
    client.cookies.set("vonk_csrf", issued.csrf)
    assert (
        client.post(
            "/api/v1/jobs",
            headers={"x-csrf-token": "wrong"},
            json=document,
        ).status_code
        == 403
    )
    assert jobs.calls == []
    assert (
        client.post(
            "/api/v1/jobs",
            headers={"x-csrf-token": issued.csrf},
            json=document,
        ).status_code
        == 202
    )


def test_cookie_authentication_is_unavailable_without_browser_service() -> None:
    """A signed bearer token in a cookie must not restore legacy cookie auth."""
    client, headers, _jobs, _audits = _client("administrator")
    client.cookies.set("vonk_session", headers["Authorization"].removeprefix("Bearer "))

    assert client.get("/api/v1/audit").status_code == 401


def test_signed_bearer_authentication_remains_unchanged_and_takes_precedence() -> None:
    """Bearer clients must remain valid and must not be resolved as cookies."""
    client, issued, _service, _sessions, _clock, codec, _jobs = _browser_client()
    client.cookies.set("vonk_session", "not-an-opaque-session")
    bearer = codec.issue(Actor("operator", "operator"), ttl_seconds=1000, now=0)

    response = client.get(
        "/api/v1/audit", headers={"authorization": f"Bearer {bearer}"}
    )

    assert response.status_code == 200
    client.cookies.set("vonk_session", issued.token)
    assert (
        client.get(
            "/api/v1/audit", headers={"authorization": f"Bearer {issued.token}"}
        ).status_code
        == 401
    )


def test_cookie_sessions_reflect_revocation_disablement_and_expiry() -> None:
    """Durable session and current-user state changes must take effect immediately."""
    for state in ("revoked", "disabled", "expired"):
        client, issued, service, sessions, clock, _codec, _jobs = _browser_client()
        client.cookies.set("vonk_session", issued.token)
        if state == "revoked":
            service.logout(issued.token)
        elif state == "disabled":
            with sessions.begin() as db:
                db.query(User).filter(User.subject == "admin").update(
                    {User.disabled_at: clock.value}
                )
        else:
            clock.value += timedelta(hours=12)

        assert client.get("/api/v1/audit").status_code == 401
