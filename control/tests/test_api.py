from dataclasses import dataclass

from fastapi.testclient import TestClient
from vonk_control.api import create_app, create_preselection_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec


@dataclass
class Enqueued:
    id: str = "job-1"
    state: str = "queued"


class Jobs:
    def __init__(self) -> None:
        self.calls = []

    def enqueue(self, kind, actor, base_commit, targets, payload, *, request_id):
        self.calls.append((kind, actor, base_commit, targets, payload, request_id))
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


def test_viewer_cannot_enqueue_mutation() -> None:
    client, headers, _, _ = _client("viewer")
    response = client.post("/api/v1/jobs", headers=headers, json={
        "kind": "probe", "base_commit": "abc", "targets": ["node"], "payload": {}
    })
    assert response.status_code == 403


def test_admin_mutation_is_correlated_and_audited() -> None:
    client, headers, jobs, audits = _client("administrator")
    response = client.post("/api/v1/jobs", headers=headers, json={
        "kind": "probe", "base_commit": "abc", "targets": ["node"], "payload": {"safe": True}
    })
    assert response.status_code == 202
    request_id = response.headers["x-request-id"]
    assert jobs.calls[0][1:4] == ("administrator", "abc", ["node"])
    event = audits.for_request(request_id)
    assert (event.actor, event.base_commit, event.targets) == ("administrator", "abc", ("node",))


def test_generic_job_endpoint_cannot_create_reconciliation_authority() -> None:
    client, headers, jobs, _audits = _client("administrator")

    response = client.post(
        "/api/v1/jobs",
        headers=headers,
        json={
            "kind": "reconcile",
            "base_commit": "a" * 40,
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
            "base_commit": "a" * 40,
            "targets": ["spk_" + "1" * 32],
            "payload": {},
        },
    )

    assert response.status_code == 422
    assert jobs.calls == []


def test_cookie_authenticated_mutation_requires_matching_csrf() -> None:
    client, headers, _, _ = _client("operator")
    token = headers["Authorization"].removeprefix("Bearer ")
    client.cookies.set("vonk_session", token)
    assert client.post("/api/v1/jobs", json={"kind": "probe", "base_commit": "abc", "targets": [], "payload": {}}).status_code == 403
    client.cookies.set("vonk_csrf", "nonce")
    assert client.post("/api/v1/jobs", headers={"x-csrf-token": "nonce"}, json={"kind": "probe", "base_commit": "abc", "targets": [], "payload": {}}).status_code == 202
