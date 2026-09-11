from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.routing import Route
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.browser_auth import BrowserAuthService
from vonk_control.catalog_api import CatalogProblem
from vonk_control.models import Base, User
from vonk_control.operation_api import BoundedErrorResponse
from vonk_control.passwords import hash_password

ADMIN_PASSWORD = "correct horse battery staple"
ADMIN_VERIFIER = hash_password(ADMIN_PASSWORD)


@dataclass
class Enqueued:
    id: str = "job-1"
    state: str = "queued"


class Jobs:
    def __init__(self) -> None:
        self.calls: list[
            tuple[str, str, str, Sequence[str], Mapping[str, object], str]
        ] = []
        self.get = self._get

    def _get(self, job_id: str) -> Enqueued:
        return Enqueued(id=job_id)

    def enqueue(
        self,
        kind: str,
        actor: str,
        authority_revision: str,
        targets: Sequence[str],
        payload: Mapping[str, object],
        *,
        request_id: str,
    ) -> Enqueued:
        self.calls.append(
            (kind, actor, authority_revision, targets, payload, request_id)
        )
        return Enqueued()

    def list(self, *, limit: int = 100) -> list[Enqueued]:
        return []

    def list_page(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        status: str | None = None,
        target: str | None = None,
    ) -> tuple[list[Enqueued], str | None, int]:
        return [], None, 0


def _client(role: str, *, agent_upgrades=None):
    codec = TokenCodec(b"k" * 32)
    audits = MemoryAuditStore()
    jobs = Jobs()
    app = create_app(
        jobs=jobs,
        tokens=codec,
        audits=audits,
        now=lambda: 10,
        agent_upgrades=agent_upgrades,
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


def _browser_client(*, agent_upgrades=None, role: str = "administrator"):
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
                role=role,
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
        now=lambda: 10,
        browser_auth=service,
        agent_upgrades=agent_upgrades,
    )
    client = TestClient(app, base_url="https://forge.example.test")
    return client, issued, service, sessions, clock, codec, jobs


def test_health_is_public_but_fleet_requires_authentication() -> None:
    client, _, _, _ = _client("viewer")
    assert client.get("/api/healthz").status_code == 200
    assert client.get("/api/fleet").status_code == 401


def test_central_api_http_errors_are_serialized_by_the_declared_models() -> None:
    client, _, _, _ = _client("viewer")

    response = client.get("/api/fleet")

    assert response.status_code == 401
    assert BoundedErrorResponse.model_validate_json(response.content).detail == (
        "authentication required"
    )
    assert response.headers["x-request-id"]
    assert response.headers["x-vonk-error-code"] == "controller.authentication_required"


def test_central_api_forbidden_error_has_distinct_safe_code() -> None:
    client, headers, _, _ = _client("viewer")

    response = client.post(
        "/api/model/qwen-code/remove",
        headers=headers,
        json={"request_key": "00000000-0000-4000-8000-000000000001"},
    )

    assert response.status_code == 403
    assert response.headers["x-vonk-error-code"] == "controller.request_rejected"
    assert response.headers["x-request-id"]


def test_unexpected_route_errors_use_bounded_context_and_request_id() -> None:
    client, headers, jobs, _ = _client("viewer")

    def fail(job_id):
        raise RuntimeError("private token and request body must stay server-side")

    jobs.get = fail
    response = client.get("/api/jobs/job-1", headers=headers)

    assert response.status_code == 500
    problem = BoundedErrorResponse.model_validate_json(response.content)
    assert problem.detail == "internal server error"
    assert problem.context is not None
    assert problem.context.code == "controller.internal_error"
    assert problem.context.http_status == 500
    assert problem.context.endpoint == "/api/jobs/job-1"
    assert problem.context.request_id == response.headers["x-request-id"]
    assert response.headers["x-vonk-error-code"] == "controller.internal_error"
    assert b"private token" not in response.content


def test_central_catalog_http_errors_are_serialized_by_catalog_problem() -> None:
    client, _, _, _ = _client("viewer")

    response = client.get("/api/catalog/source-bundles/" + "a" * 64)

    assert response.status_code == 401
    problem = CatalogProblem.model_validate_json(response.content)
    assert problem.code == "catalog.authentication_required"
    assert problem.detail == "authentication required"


def test_request_boundary_admits_large_recipe_images_only_on_exact_put_route() -> None:
    client, _, _, _ = _client("viewer")
    build_id = "00000000-0000-4000-8000-000000000001"
    route = f"/agent/recipe-builds/{build_id}/image"
    body = b"x" * 1_048_577

    # The missing trusted-proxy identity is rejected after the request-size
    # boundary, proving that the protocol's streaming image route was admitted.
    assert client.put(route, content=body).status_code == 401

    # Method and path are both part of the authority boundary. Near-matches
    # retain the ordinary one-MiB API ceiling.
    assert client.post(route, content=body).status_code == 413
    assert client.put(f"{route}/extra", content=body).status_code == 413


def test_removed_package_and_deployment_routes_are_not_registered() -> None:
    client, _, _, _ = _client("administrator")
    package_prefix = "/api/" + "packages/"
    deployment_prefix = "/api/" + "deployments"
    app = client.app
    assert isinstance(app, FastAPI)
    legacy_paths = {
        route.path
        for route in app.routes
        if isinstance(route, Route)
        and (
            route.path.startswith(package_prefix)
            or route.path.startswith(deployment_prefix)
        )
    }
    assert legacy_paths == set()


def test_generic_job_submission_route_is_retired() -> None:
    client, headers, jobs, _audits = _client("administrator")

    assert client.post(
        "/api/jobs",
        headers=headers,
        json={"kind": "probe", "authority_revision": "abc", "targets": [], "payload": {}},
    ).status_code == 405
    assert jobs.calls == []


def test_cookie_authentication_resolves_only_through_browser_sessions() -> None:
    """Opaque browser tokens must work without being signed bearer tokens."""
    client, issued, _service, _sessions, _clock, _codec, _jobs = _browser_client()
    client.cookies.set("vonk_session", issued.token)

    response = client.get("/api/audit")

    assert response.status_code == 200


def test_cookie_authenticated_mutation_requires_matching_csrf() -> None:
    client, issued, _service, _sessions, _clock, _codec, jobs = _browser_client()
    client.cookies.set("vonk_session", issued.token)
    document = {"name": "CSRF test", "assignments": []}

    assert client.put("/api/profile/1", json=document).status_code == 403
    client.cookies.set("vonk_csrf", issued.csrf)
    assert (
        client.put(
            "/api/profile/1",
            headers={"x-csrf-token": "wrong"},
            json=document,
        ).status_code
        == 403
    )
    assert jobs.calls == []
    assert (
        client.put(
            "/api/profile/1",
            headers={"x-csrf-token": issued.csrf},
            json=document,
        ).status_code
        == 503
    )


def test_cookie_authentication_is_unavailable_without_browser_service() -> None:
    """A signed bearer token in a cookie must not restore legacy cookie auth."""
    client, headers, _jobs, _audits = _client("administrator")
    client.cookies.set("vonk_session", headers["Authorization"].removeprefix("Bearer "))

    assert client.get("/api/audit").status_code == 401


def test_signed_bearer_authentication_remains_unchanged_and_takes_precedence() -> None:
    """Bearer clients must remain valid and must not be resolved as cookies."""
    client, issued, _service, _sessions, _clock, codec, _jobs = _browser_client()
    client.cookies.set("vonk_session", "not-an-opaque-session")
    bearer = codec.issue(Actor("operator", "operator"), ttl_seconds=1000, now=0)

    response = client.get(
        "/api/audit", headers={"authorization": f"Bearer {bearer}"}
    )

    assert response.status_code == 200
    client.cookies.set("vonk_session", issued.token)
    assert (
        client.get(
            "/api/audit", headers={"authorization": f"Bearer {issued.token}"}
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

        assert client.get("/api/audit").status_code == 401


class _RejectedPlan(BaseModel):
    """A stand-in for the compiled plans whose failures are reported."""

    retries: int


def test_bounded_error_detail_never_echoes_the_rejected_value() -> None:
    """The bounded error detail is redacted as well as truncated.

    Routes build this detail from a caught exception, and a pydantic
    ``ValidationError`` stringifies the submitted input, so an unredacted
    detail would hand the failing value straight back to the caller.
    """
    from vonk_control.api import _bounded_error_content

    with pytest.raises(ValidationError) as rejected:
        _RejectedPlan.model_validate({"retries": "token=super-secret-value"})
    body = json.loads(_bounded_error_content(str(rejected.value)))
    assert "super-secret-value" not in body["detail"]
    assert "token=<redacted>" in body["detail"]

    assert len(json.loads(_bounded_error_content("x" * 400))["detail"]) == 256
