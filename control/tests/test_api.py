import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.agent_upgrades import AgentUpgradePlan
from vonk_control.api import create_app
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
        self.calls.append(
            (kind, actor, authority_revision, targets, payload, request_id)
        )
        return Enqueued()

    def get(self, job_id):
        return Enqueued(id=job_id)


def _client(role: str, *, generic_jobs_enabled: bool = True, agent_upgrades=None):
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
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
        generic_jobs_enabled=True,
        browser_auth=service,
        agent_upgrades=agent_upgrades,
    )
    client = TestClient(app, base_url="https://forge.example.test")
    return client, issued, service, sessions, clock, codec, jobs


class RepairPreviewUpgrades:
    def __init__(self, current_package: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[object, object, object, object]] = []
        self._current_package = current_package

    def current_package(self):
        assert self._current_package is not None
        return dict(self._current_package)

    def preview(self, node_ids, package, *, repair_manifest=None, strategy):
        self.calls.append((node_ids, package, repair_manifest, strategy))
        return AgentUpgradePlan(
            authority_revision="c" * 64,
            node_ids=tuple(node_ids),
            package=dict(package),
            plan_digest="d" * 64,
            repair_manifest=(
                None if repair_manifest is None else dict(repair_manifest)
            ),
            strategy=strategy,
        )


def repair_preview_document() -> dict[str, object]:
    node_id = "spk_" + "a" * 32
    authority = "1" * 64
    package_sha = "2" * 64
    package_url = (
        f"https://install.vonkforge.ai/repair-capsules/{node_id}/{authority}/"
        f"{package_sha}/vonk-forge-agent.deb"
    )
    package = {
        "architecture": "linux-arm64",
        "package_bytes": 6000000,
        "package_sha256": package_sha,
        "package_signature": "3" * 128,
        "package_url": package_url,
        "package_version": "0.1.0~dev.382+gd1cef9c7d1ce",
        "schema_version": 1,
        "target_binary_digest": "4" * 64,
        "target_build_digest": "sha256:" + "5" * 64,
    }
    manifest = {
        "schema_version": 2,
        "kind": "agent-upgrade-repair",
        "node_id": node_id,
        "authority_sha256": authority,
        "package": package,
    }
    return {
        "node_ids": [node_id],
        "repair_manifest": manifest,
        "strategy": "one-at-a-time",
    }


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


def test_telemetry_request_boundary_allows_one_mib_and_rejects_larger_payloads() -> None:
    client, _, _, _ = _client("viewer")
    prefix = b'{"payload":"'
    suffix = b'"}'
    body = prefix + b"x" * (1_048_576 - len(prefix) - len(suffix)) + suffix

    # The payload passes the size and duplicate-key boundary, then reaches the
    # authenticated agent route. No trusted agent identity is supplied here.
    assert len(body) == 1_048_576
    assert client.post("/agent/v1/telemetry", content=body).status_code == 401
    assert (
        client.post("/agent/v1/telemetry", content=body + b"x").status_code
        == 413
    )


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
    assert event.occurred_at is not None
    assert (event.actor, event.authority_revision, event.targets) == (
        "administrator",
        "abc",
        ("node",),
    )
    audit_response = client.get("/api/v1/audit", headers=headers)
    assert audit_response.status_code == 200
    assert (
        audit_response.json()["events"][0]["occurred_at"]
        == event.occurred_at.isoformat()
    )


def test_generic_job_endpoint_cannot_create_reconciliation_authority() -> None:
    client, headers, jobs, _audits = _client("administrator")

    response = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "kind": "reconcile",
            "authority_revision": "a" * 64,
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
            "authority_revision": "a" * 64,
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


def test_browser_admin_can_preview_node_bound_repair_with_matching_csrf() -> None:
    upgrades = RepairPreviewUpgrades()
    client, issued, *_ = _browser_client(agent_upgrades=upgrades)
    client.cookies.set("vonk_session", issued.token)
    client.cookies.set("vonk_csrf", issued.csrf)
    document = repair_preview_document()

    response = client.post(
        "/api/v1/agents/upgrades/preview",
        headers={"x-csrf-token": issued.csrf},
        json=document,
    )

    assert response.status_code == 200
    assert response.json()["repair_manifest"] == document["repair_manifest"]
    assert upgrades.calls == [
        (
            document["node_ids"],
            document["repair_manifest"]["package"],
            document["repair_manifest"],
            "one-at-a-time",
        )
    ]


def test_browser_repair_preview_rejects_legacy_schema_one() -> None:
    upgrades = RepairPreviewUpgrades()
    client, headers, *_ = _client("administrator", agent_upgrades=upgrades)
    document = repair_preview_document()
    document["repair_manifest"]["schema_version"] = 1

    response = client.post(
        "/api/v1/agents/upgrades/preview",
        headers=headers,
        json=document,
    )

    assert response.status_code == 422
    assert upgrades.calls == []


def test_browser_repair_preview_requires_csrf_and_administrator_role() -> None:
    upgrades = RepairPreviewUpgrades()
    client, issued, *_ = _browser_client(agent_upgrades=upgrades)
    client.cookies.set("vonk_session", issued.token)
    assert (
        client.post(
            "/api/v1/agents/upgrades/preview", json=repair_preview_document()
        ).status_code
        == 403
    )
    assert upgrades.calls == []

    operator_upgrades = RepairPreviewUpgrades()
    operator, operator_headers, *_ = _client(
        "operator", agent_upgrades=operator_upgrades
    )
    assert (
        operator.post(
            "/api/v1/agents/upgrades/preview",
            headers=operator_headers,
            json=repair_preview_document(),
        ).status_code
        == 403
    )
    assert operator_upgrades.calls == []


def test_upgrade_preview_accepts_current_package_echo_but_rejects_unsigned_custom() -> (
    None
):
    repair_document = repair_preview_document()
    current_package = repair_document["repair_manifest"]["package"]
    upgrades = RepairPreviewUpgrades(current_package=current_package)
    client, issued, *_ = _browser_client(agent_upgrades=upgrades)
    client.cookies.set("vonk_session", issued.token)
    client.cookies.set("vonk_csrf", issued.csrf)
    headers = {"x-csrf-token": issued.csrf}
    ordinary = {
        "node_ids": repair_document["node_ids"],
        "package": current_package,
        "strategy": "one-at-a-time",
    }

    assert (
        client.post(
            "/api/v1/agents/upgrades/preview", headers=headers, json=ordinary
        ).status_code
        == 200
    )
    custom = {**current_package, "package_signature": "f" * 128}
    response = client.post(
        "/api/v1/agents/upgrades/preview",
        headers=headers,
        json={**ordinary, "package": custom},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "a custom agent package requires its node-bound repair manifest"
    )
    assert len(upgrades.calls) == 1


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
