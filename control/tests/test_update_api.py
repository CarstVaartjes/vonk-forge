from __future__ import annotations

from dataclasses import dataclass, field

from fastapi.testclient import TestClient
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec


class Jobs:
    def get(self, job_id: str):
        raise KeyError(job_id)

    def list(self, *, limit: int = 100):
        del limit
        return []

    def list_page(self, **_kwargs):
        return [], None, 0


@dataclass
class Updates:
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def skew(self) -> dict[str, object]:
        return {
            "affected_nodes": ["spk_" + "1" * 32],
            "digest": "sha256:" + "a" * 64,
            "incompatible_nodes": [],
            "nodes": [],
            "offline_pending": [],
            "prompt_required": True,
            "target": {
                "build_digest": "sha256:" + "b" * 64,
                "platform_version": "2.0.0",
                "protocol_maximum": 2,
                "protocol_minimum": 1,
                "release": "platform/releases/2.0.0/" + "b" * 64 + ".json",
                "release_digest": "sha256:" + "b" * 64,
                "target_sha256": "b" * 64,
                "tuf_targets_version": 7,
            },
        }

    def plan(
        self,
        *,
        release: str,
    ) -> dict[str, object]:
        self.calls.append(("plan", release))
        return {
            "affected_routes": [],
            "batches": [["spk_" + "1" * 32]],
            "canary_node": "spk_" + "1" * 32,
            "gates": [],
            "incompatible": [],
            "offline_pending": [],
            "plan_digest": "sha256:" + "d" * 64,
            "rollback_slots": {"spk_" + "1" * 32: "B"},
            "soak_seconds": 300,
            "target": {
                "build_digest": "sha256:" + "b" * 64,
                "platform_version": "2.0.0",
                "protocol_maximum": 2,
                "protocol_minimum": 1,
                "release": "platform/releases/2.0.0/" + "b" * 64 + ".json",
                "release_digest": "sha256:" + "b" * 64,
                "target_sha256": "b" * 64,
                "tuf_targets_version": 7,
            },
            "workloads": [],
        }

    def apply(self, plan_digest: str, actor: str, request_id: str):
        self.calls.append(("apply", plan_digest, actor, request_id))
        return {
            "batches": [["spk_" + "1" * 32]],
            "can_approve_resume": False,
            "current_batch": 0,
            "failure_reason": None,
            "id": "10000000-0000-4000-8000-000000000001",
            "job_id": "40000000-0000-4000-8000-000000000004",
            "nodes": [{"node_id": "spk_" + "1" * 32, "state": "pending"}],
            "plan_digest": plan_digest,
            "required_action": None,
            "resume_required": False,
            "state": "planned",
        }

    def status(self, rollout_id: str):
        self.calls.append(("status", rollout_id))
        return {
            "batches": [["spk_" + "1" * 32]],
            "can_approve_resume": False,
            "current_batch": 0,
            "failure_reason": None,
            "id": rollout_id,
            "job_id": "40000000-0000-4000-8000-000000000004",
            "nodes": [{"node_id": "spk_" + "1" * 32, "state": "pending"}],
            "plan_digest": "sha256:" + "d" * 64,
            "required_action": None,
            "resume_required": False,
            "state": "planned",
        }

    def approve_resume(self, rollout_id: str, actor: str, request_id: str, reason: str):
        self.calls.append(("approve", rollout_id, actor, request_id, reason))
        return {
            "batches": [["spk_" + "1" * 32]],
            "can_approve_resume": False,
            "current_batch": 0,
            "failure_reason": None,
            "id": rollout_id,
            "job_id": "40000000-0000-4000-8000-000000000004",
            "nodes": [{"node_id": "spk_" + "1" * 32, "state": "pending"}],
            "plan_digest": "sha256:" + "d" * 64,
            "required_action": None,
            "resume_required": False,
            "state": "planned",
        }


def _client(role: str, updates: Updates | None = None):
    codec = TokenCodec(b"x" * 32)
    service = updates or Updates()
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
        updates=service,
    )
    token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
    return TestClient(app), {"Authorization": f"Bearer {token}"}, service, audits


def test_authenticated_update_reads_expose_skew_and_rollout_status() -> None:
    client, headers, updates, _audits = _client("viewer")
    rollout_id = "10000000-0000-4000-8000-000000000001"

    skew = client.get("/api/v1/updates/skew", headers=headers)
    status = client.get(f"/api/v1/updates/{rollout_id}", headers=headers)

    assert skew.status_code == 200
    assert skew.json()["prompt_required"] is True
    assert status.status_code == 200
    assert status.json()["id"] == rollout_id
    assert status.json()["job_id"] == "40000000-0000-4000-8000-000000000004"
    assert updates.calls == [("status", rollout_id)]


def test_operator_plans_and_applies_only_the_exact_server_digest() -> None:
    client, headers, updates, audits = _client("operator")
    request_id = "20000000-0000-4000-8000-000000000002"
    plan = client.post(
        "/api/v1/updates/plan",
        headers=headers,
        json={
            "release": "platform/releases/2.0.0/" + "b" * 64 + ".json"
        },
    )
    digest = plan.json()["plan_digest"]

    applied = client.post(
        "/api/v1/updates",
        headers={**headers, "x-request-id": request_id},
        json={"plan_digest": digest},
    )

    assert plan.status_code == 200
    assert applied.status_code == 202
    assert applied.json()["id"] == "10000000-0000-4000-8000-000000000001"
    assert applied.json()["plan_digest"] == digest
    assert applied.json()["job_id"] == "40000000-0000-4000-8000-000000000004"
    assert updates.calls[:2] == [
        ("plan", "platform/releases/2.0.0/" + "b" * 64 + ".json"),
        ("apply", digest, "operator", request_id),
    ]
    event = audits.for_request(request_id)
    assert (event.action, event.actor) == ("platform.update.apply", "operator")


def test_viewer_cannot_plan_or_apply_and_operator_cannot_approve_resume() -> None:
    viewer, viewer_headers, _updates, _audits = _client("viewer")
    operator, operator_headers, _updates, _audits = _client("operator")
    rollout_id = "10000000-0000-4000-8000-000000000001"

    assert viewer.post(
        "/api/v1/updates/plan",
        headers=viewer_headers,
        json={
            "release": "platform/releases/2.0.0/" + "b" * 64 + ".json"
        },
    ).status_code == 403
    assert viewer.post(
        "/api/v1/updates",
        headers=viewer_headers,
        json={"plan_digest": "sha256:" + "d" * 64},
    ).status_code == 403
    assert operator.post(
        f"/api/v1/updates/{rollout_id}/approve-resume",
        headers=operator_headers,
        json={"reason": "rollback verified"},
    ).status_code == 403


def test_administrator_approval_is_correlated_and_audited() -> None:
    client, headers, updates, audits = _client("administrator")
    rollout_id = "10000000-0000-4000-8000-000000000001"
    request_id = "30000000-0000-4000-8000-000000000003"

    response = client.post(
        f"/api/v1/updates/{rollout_id}/approve-resume",
        headers={**headers, "x-request-id": request_id},
        json={"reason": "rollback verified"},
    )

    assert response.status_code == 202
    assert updates.calls == [
        ("approve", rollout_id, "administrator", request_id, "rollback verified")
    ]
    event = audits.for_request(request_id)
    assert (event.action, event.actor) == (
        "platform.update.approve-resume",
        "administrator",
    )


def test_update_api_fails_closed_without_services_and_has_no_online_host_update() -> None:
    codec = TokenCodec(b"x" * 32)
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
    )
    token = codec.issue(Actor("operator", "operator"), ttl_seconds=100, now=0)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    assert client.get("/api/v1/updates/skew", headers=headers).status_code == 503
    assert client.post(
        "/api/v1/updates/host/apply", headers=headers, json={}
    ).status_code == 404


def test_update_routes_emit_the_exact_shared_cli_and_web_contracts() -> None:
    client, headers, _updates, _audits = _client("administrator")
    release = "platform/releases/2.0.0/" + "b" * 64 + ".json"
    rollout_id = "10000000-0000-4000-8000-000000000001"

    skew = client.get("/api/v1/updates/skew", headers=headers).json()
    plan = client.post(
        "/api/v1/updates/plan", headers=headers, json={"release": release}
    ).json()
    rollout = client.post(
        "/api/v1/updates",
        headers=headers,
        json={"plan_digest": plan["plan_digest"]},
    ).json()
    resumed = client.post(
        f"/api/v1/updates/{rollout_id}/approve-resume",
        headers=headers,
        json={},
    ).json()

    assert set(skew) == {
        "affected_nodes",
        "digest",
        "incompatible_nodes",
        "nodes",
        "offline_pending",
        "prompt_required",
        "target",
    }
    assert set(skew["target"]) == {
        "build_digest",
        "platform_version",
        "protocol_maximum",
        "protocol_minimum",
        "release",
        "release_digest",
        "target_sha256",
        "tuf_targets_version",
    }
    assert set(plan) == {
        "affected_routes",
        "batches",
        "canary_node",
        "gates",
        "incompatible",
        "offline_pending",
        "plan_digest",
        "rollback_slots",
        "soak_seconds",
        "target",
        "workloads",
    }
    expected_rollout = {
        "batches",
        "can_approve_resume",
        "current_batch",
        "failure_reason",
        "id",
        "job_id",
        "nodes",
        "plan_digest",
        "required_action",
        "resume_required",
        "state",
    }
    assert set(rollout) == expected_rollout
    assert set(resumed) == expected_rollout
