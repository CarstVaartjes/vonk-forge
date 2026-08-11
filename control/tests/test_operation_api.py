from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control import operation_api
from vonk_control.api import AdminServices, create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentPresence,
    Base,
    Job,
    Reconciliation,
    RoutePublication,
    RoutePublicationOwner,
)
from vonk_control.operation_api import JobProgress, OperationApiServices, OperationPage

COMMIT = "a" * 40
DIGEST = "d" * 64
NODE_ID = "spk_" + "1" * 32


@dataclass
class EnqueuedJob:
    id: str = "11111111-1111-4111-8111-111111111111"
    state: str = "queued"
    kind: str = "reconcile"
    base_commit: str = COMMIT
    targets: tuple[str, ...] = (NODE_ID,)
    current_attempt: int = 1
    status_reason: str | None = None
    reconciliation_id: str | None = "22222222-2222-4222-8222-222222222222"


class Jobs:
    def __init__(self) -> None:
        self.job = EnqueuedJob()

    def enqueue(self, *_args, **_kwargs):
        return self.job

    def get(self, job_id):
        if job_id != self.job.id:
            raise KeyError(job_id)
        return self.job

    def list(self, *, limit=100):
        return []

    def list_page(self, *, limit=100, cursor=None, status=None, target=None):
        del limit, cursor, status, target
        return [], None, 0


class Repository:
    def head(self):
        return COMMIT


class Reconciler:
    def __init__(self) -> None:
        self.planned: list[tuple[str, str]] = []

    def plan(self, commit, profile_id, *, fleet_evidence_digest):
        del fleet_evidence_digest
        self.planned.append((commit, profile_id))
        return type(
            "Plan",
            (),
            {
                "commit": commit,
                "digest": DIGEST,
                "targets": (NODE_ID,),
                "placements": {"model": (NODE_ID,)},
                "routes": {},
                "releases": {},
                "input_digests": {"fleet": "f" * 64},
                "operation_graph": type(
                    "Graph",
                    (),
                    {
                        "reconciliation_id": "22222222-2222-4222-8222-222222222222",
                        "document": {
                            "base_commit": commit,
                            "nodes": [],
                            "schema_version": 1,
                            "targets": [NODE_ID],
                        },
                    },
                )(),
                "agent_protocol_range": (1, 1),
            },
        )()

    def enqueue(self, plan_digest, actor, request_id, **_kwargs):
        if plan_digest != DIGEST:
            raise ValueError("unknown reconciliation plan digest")
        return {
            "base_commit": COMMIT,
            "job_id": "11111111-1111-4111-8111-111111111111",
            "reconciliation_id": "22222222-2222-4222-8222-222222222222",
            "state": "queued",
        }


def _client(*, fleet=None, operations=None, role="operator"):
    codec = TokenCodec(b"k" * 32)
    reconciler = Reconciler()
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet=fleet or (lambda: {"commit": COMMIT, "nodes": []}),
        now=lambda: 10,
        admin=AdminServices(
            repository=Repository(),
            proposals=None,
            changes=None,
            reconciler=reconciler,
        ),
        operations=operations,
    )
    token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
    return (
        TestClient(app),
        {"Authorization": f"Bearer {token}"},
        reconciler,
        audits,
    )


def test_profile_plan_is_a_strict_view_of_canonical_server_plan() -> None:
    client, operator, reconciler, _audits = _client()

    canonical = client.post(
        "/api/v1/reconciliations/plan",
        headers=operator,
        json={"commit": COMMIT, "profile_id": "agent"},
    )
    scoped = client.post("/api/v1/profiles/agent/plan", headers=operator)

    assert scoped.status_code == 200
    assert scoped.content == canonical.content
    assert reconciler.planned == [(COMMIT, "agent"), (COMMIT, "agent")]
    assert client.post(
        "/api/v1/profiles/agent/plan",
        headers=operator,
        json={"commit": "0" * 40},
    ).status_code == 422


def test_apply_requires_exact_server_plan_digest() -> None:
    client, operator, _reconciler, audits = _client()
    plan = client.post("/api/v1/profiles/agent/plan", headers=operator)
    assert plan.status_code == 200
    fleet = client.get("/api/v1/fleet", headers=operator)
    assert fleet.status_code == 200
    assert plan.json()["fleet_evidence_digest"] == fleet.json()["evidence_digest"]

    stale = client.post(
        "/api/v1/reconciliations",
        headers=operator,
        json={
            "fleet_evidence_digest": plan.json()["fleet_evidence_digest"],
            "plan_digest": "0" * 64,
        },
    )
    accepted = client.post(
        "/api/v1/reconciliations",
        headers=operator,
        json={
            "fleet_evidence_digest": plan.json()["fleet_evidence_digest"],
            "plan_digest": plan.json()["digest"],
        },
    )

    assert stale.status_code == 409
    assert stale.json() == {"detail": "reconciliation plan digest is stale"}
    assert accepted.status_code == 202
    assert accepted.json()["base_commit"] == COMMIT
    assert audits.for_request(accepted.headers["x-request-id"]).action == (
        "reconciliation.apply"
    )


def test_apply_rejects_live_evidence_changed_after_plan() -> None:
    fleet_state = {"commit": COMMIT, "nodes": []}
    client, operator, _reconciler, audits = _client(fleet=lambda: fleet_state)
    plan = client.post("/api/v1/profiles/agent/plan", headers=operator)
    assert plan.status_code == 200
    planned_evidence = plan.json()["fleet_evidence_digest"]

    fleet_state["nodes"] = [
        {
            "agent_online": False,
            "agent_state": "unavailable",
            "compatibility": "incompatible",
            "disk_available_bytes": 0,
            "display_name": "Unavailable",
            "healthy": False,
            "hostname": "unavailable.invalid",
            "id": NODE_ID,
            "labels": {},
            "lifecycle": "managed",
            "memory_available_bytes": 0,
            "profile": "agent",
            "stale": True,
        }
    ]
    changed = client.get("/api/v1/fleet", headers=operator).json()
    assert changed["evidence_digest"] != planned_evidence

    response = client.post(
        "/api/v1/reconciliations",
        headers=operator,
        json={
            "fleet_evidence_digest": planned_evidence,
            "plan_digest": plan.json()["digest"],
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "fleet acceptance evidence is stale"}
    with pytest.raises(KeyError):
        audits.for_request(response.headers["x-request-id"])


def test_fleet_evidence_digest_is_deterministic_over_validated_public_projection() -> None:
    first_client, operator, *_ = _client()
    second_client, second_operator, *_ = _client()

    first = first_client.get("/api/v1/fleet", headers=operator).json()
    second = second_client.get("/api/v1/nodes/status", headers=second_operator).json()

    assert first["evidence_digest"] == second["evidence_digest"]
    assert len(first["evidence_digest"]) == 64


def test_nodes_status_marks_missing_observation_unknown_and_stale() -> None:
    def fleet():
        return {
            "commit": COMMIT,
            "nodes": [
                {
                    "id": NODE_ID,
                    "display_name": "Alpha",
                    "hostname": "alpha",
                    "lifecycle": "ready",
                    "healthy": None,
                    "labels": {},
                    "profile": None,
                    "memory_available_bytes": 0,
                    "disk_available_bytes": 0,
                    "probe_age_seconds": None,
                    "stale": True,
                }
            ],
        }

    client, operator, _reconciler, _audits = _client(fleet=fleet)

    response = client.get("/api/v1/nodes/status", headers=operator)

    assert response.status_code == 200
    node = response.json()["nodes"][0]
    assert node["healthy"] is None
    assert node["stale"] is True
    assert node["probe_age_seconds"] is None
    assert "management" not in json.dumps(response.json(), sort_keys=True)


def test_optional_operation_projections_fail_closed_when_unavailable() -> None:
    client, operator, _reconciler, _audits = _client()

    endpoint = client.get("/api/v1/endpoints/model-a", headers=operator)
    agents = client.get("/api/v1/agents", headers=operator)

    assert endpoint.status_code == 503
    assert endpoint.json() == {"detail": "endpoint publication unavailable"}
    assert agents.status_code == 503
    assert agents.json() == {"detail": "agent projection unavailable"}


def test_job_status_has_typed_progress_fields_without_payloads() -> None:
    client, operator, _reconciler, _audits = _client()

    response = client.get(
        "/api/v1/jobs/11111111-1111-4111-8111-111111111111",
        headers=operator,
    )

    assert response.status_code == 200
    assert response.json() == {
        "base_commit": COMMIT,
        "current_attempt": 1,
        "id": "11111111-1111-4111-8111-111111111111",
        "kind": "reconcile",
            "operations": [],
            "operation_next_cursor": None,
            "operation_total": 0,
        "progress": {"completed": 0, "failed": 0, "running": 0, "total": 0},
        "reconciliation_id": "22222222-2222-4222-8222-222222222222",
        "state": "queued",
        "status_reason": None,
            "targets": [NODE_ID],
            "target_next_cursor": None,
            "target_total": 1,
    }
    encoded = json.dumps(response.json(), sort_keys=True)
    assert "payload" not in encoded
    assert "result" not in encoded


def test_plan_response_whitelists_nested_route_release_and_dag_fields() -> None:
    public_digest = "1" * 64
    plan = SimpleNamespace(
        commit=COMMIT,
        digest=DIGEST,
        targets=(NODE_ID,),
        placements={"model": (NODE_ID,)},
        routes={
            "model": {
                "workload_id": "model",
                "nodes": (NODE_ID,),
                "entrypoint_node_id": NODE_ID,
                "scheme": "http",
                "port": 8000,
                "path": "/v1",
                "quota": {
                    "requests_per_minute": 10,
                    "tokens_per_minute": 1000,
                },
                "quota_digest": public_digest,
                "private_evidence": "route-secret",
            }
        },
        releases={
            "model": {
                "manifest_path": "manifests/releases/model.json",
                "manifest_sha256": public_digest,
                "definition_hash": public_digest,
                "release_request": {
                    "schema_version": 1,
                    "target_name": "model",
                    "oci_manifest_digest": "sha256:" + "2" * 64,
                    "target_digest": "3" * 64,
                    "provenance_digest": "4" * 64,
                    "adapter_id": "systemd",
                },
                "workload_requests": {
                    "prepare": {
                        "schema_version": 1,
                        "workload_id": "model",
                        "release_digest": "3" * 64,
                        "adapter_id": "systemd",
                        "profile_digest": "5" * 64,
                    },
                    "start": {
                        "schema_version": 1,
                        "workload_id": "model",
                        "release_digest": "3" * 64,
                        "adapter_id": "systemd",
                        "preparation_digest": "6" * 64,
                    },
                    "stop": {
                        "schema_version": 1,
                        "workload_id": "model",
                        "release_digest": "3" * 64,
                        "adapter_id": "systemd",
                    },
                    "health": {
                        "schema_version": 1,
                        "workload_id": "model",
                        "release_digest": "3" * 64,
                        "adapter_id": "systemd",
                    },
                    "verify": {
                        "schema_version": 1,
                        "workload_id": "model",
                        "release_digest": "3" * 64,
                        "adapter_id": "systemd",
                        "expected_digest": "7" * 64,
                    },
                },
                "endpoint": {"scheme": "http", "port": 8000, "path": "/v1"},
                "private_evidence": "release-secret",
            }
        },
        input_digests={"inventory/fleet.toml": "8" * 64},
        operation_graph=SimpleNamespace(
            reconciliation_id="22222222-2222-4222-8222-222222222222",
            document={
                "schema_version": 1,
                "base_commit": COMMIT,
                "targets": [NODE_ID],
                "nodes": [
                    {
                        "operation_id": "model:node.probe",
                        "node_id": NODE_ID,
                        "workload_id": "model",
                        "kind": "node.probe",
                        "dependencies": [],
                        "compensation_kind": None,
                        "payload_digest": "9" * 64,
                        "private_evidence": "operation-secret",
                    }
                ],
                "private_evidence": "graph-secret",
            },
        ),
        agent_protocol_range=(1, 1),
    )

    response = operation_api.plan_response(
        plan, fleet_evidence_digest="f" * 64
    ).model_dump(mode="json")

    assert response["placements"] == {"model": [NODE_ID]}
    assert response["routes"]["model"]["quota"] == {
        "requests_per_minute": 10,
        "tokens_per_minute": 1000,
    }
    assert response["releases"]["model"]["release_request"]["adapter_id"] == (
        "systemd"
    )
    assert response["operation_graph"]["nodes"] == [
        {
            "operation_id": "model:node.probe",
            "node_id": NODE_ID,
            "workload_id": "model",
            "kind": "node.probe",
            "dependencies": [],
            "compensation_kind": None,
            "payload_digest": "9" * 64,
        }
    ]
    assert "private_evidence" not in json.dumps(response, sort_keys=True)


def test_job_operation_progress_projects_only_bounded_phase() -> None:
    operations = OperationApiServices(
        endpoint=lambda _alias: {},
        agents=lambda: (),
        job_operations=lambda _job_id, _cursor, _limit: OperationPage(({
                "attempt": 1,
                "graph_operation_id": "model:node.probe",
                "id": "44444444-4444-4444-8444-444444444444",
                "kind": "node.probe",
                "node_id": NODE_ID,
                "progress": {
                    "phase": "checking",
                    "private_evidence": "must-not-cross-api",
                },
                "state": "running",
                "updated_at": "2026-08-05T12:00:00+00:00",
            },), None, JobProgress(completed=0, failed=0, running=1, total=1)),
        resume_job=lambda _job_id: None,
    )
    client, operator, _reconciler, _audits = _client(operations=operations)

    response = client.get(
        "/api/v1/jobs/11111111-1111-4111-8111-111111111111",
        headers=operator,
    )

    assert response.status_code == 200
    assert response.json()["operations"][0]["progress"] == {
        "phase": "checking"
    }
    assert "private_evidence" not in response.text


def _encoded(document: dict[str, object]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_durable_projection_reads_only_current_activation_and_hides_agent_secrets(
    tmp_path,
) -> None:
    projection_factory = getattr(operation_api, "durable_operation_services", None)
    assert callable(projection_factory)
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    reconciliation_id = "22222222-2222-4222-8222-222222222222"
    route_document = {
        "generation": 7,
        "routes": {
            "model-a": {
                "address": "10.0.0.42",
                "evidence_digest": "e" * 64,
                "node_id": NODE_ID,
                "observed_at": now.isoformat(),
                "operation_id": f"model-a:{NODE_ID}:workload.verify",
                "path": "/v1",
                "port": 8000,
                "scheme": "http",
                "verify_evidence_digest": "v" * 64,
            }
        },
        "schema_version": 1,
        "state": "published",
    }
    route_bytes = _encoded(route_document)
    litellm_bytes = _encoded({"model_list": []})
    issued_at = now.isoformat()
    expires_at = (now + timedelta(minutes=5)).isoformat()
    manifest_document = {
        "schema_version": 1,
        "generation": 7,
        "state": "published",
        "reconciliation_id": reconciliation_id,
        "plan_digest": DIGEST,
        "evidence_set_digest": "e" * 64,
        "routes_sha256": hashlib.sha256(route_bytes).hexdigest(),
        "litellm_sha256": hashlib.sha256(litellm_bytes).hexdigest(),
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    manifest_bytes = _encoded(manifest_document)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    marker = {
        "schema_version": 1,
        "generation": 7,
        "state": "published",
        "reconciliation_id": reconciliation_id,
        "plan_digest": DIGEST,
        "evidence_set_digest": "e" * 64,
        "routes_sha256": hashlib.sha256(route_bytes).hexdigest(),
        "litellm_sha256": hashlib.sha256(litellm_bytes).hexdigest(),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "directory": f"00000007-{manifest_digest}",
        "manifest_sha256": manifest_digest,
    }
    marker_bytes = _encoded(marker)
    route_root = tmp_path / "routes"
    generation = route_root / "generations" / marker["directory"]
    generation.mkdir(parents=True)
    (route_root / "activation.json").write_bytes(marker_bytes)
    (generation / "manifest.json").write_bytes(manifest_bytes)
    (generation / "routes.json").write_bytes(route_bytes)
    (generation / "litellm.json").write_bytes(litellm_bytes)

    engine = create_engine(f"sqlite:///{tmp_path / 'operations.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            Reconciliation(
                id=reconciliation_id,
                base_commit=COMMIT,
                status="succeeded",
                summary={},
                graph={
                    "base_commit": COMMIT,
                    "nodes": [],
                    "schema_version": 1,
                    "targets": [NODE_ID],
                },
                graph_digest="3" * 64,
                plan_digest=DIGEST,
                current_phase="completed",
                created_at=now,
            )
        )
        session.add(
            RoutePublication(
                reconciliation_id=reconciliation_id,
                state="completed",
                generation=7,
                plan_digest=DIGEST,
                evidence_digest=marker["evidence_set_digest"],
                activation_marker=marker,
                activation_marker_digest=hashlib.sha256(marker_bytes).hexdigest(),
                route_digest=marker["routes_sha256"],
                litellm_digest=marker["litellm_sha256"],
                bundle_digest=marker["manifest_sha256"],
                lease_issued_at=now,
                lease_expires_at=now + timedelta(minutes=5),
            )
        )
        session.add(
            RoutePublicationOwner(
                singleton_id=1,
                reconciliation_id=reconciliation_id,
                owner_generation=7,
                updated_at=now,
            )
        )
        session.add(
            AgentNode(
                node_id=NODE_ID,
                state="active",
                protocol_version=1,
                capabilities=["node.probe"],
                last_seen_at=now,
            )
        )
        session.add(
            AgentCertificate(
                serial="serial-secret",
                node_id=NODE_ID,
                not_before=now - timedelta(days=1),
                not_after=now + timedelta(days=30),
                fingerprint="fingerprint-secret",
                certificate_pem="certificate-body-secret",
                chain_pem="chain-body-secret",
                state="active",
                generation=1,
            )
        )
        session.add(
            AgentPresence(
                node_id=NODE_ID,
                certificate_serial="serial-secret",
                certificate_fingerprint="fingerprint-secret",
                management_address="10.0.0.42",
                observed_at=now,
            )
        )

        services = projection_factory(
            sessions,
            route_root,
            clock=lambda: now,
            cursors=TokenCodec(b"k" * 32).cursor_codec(),
        )
    endpoint = services.endpoint("model-a")
    agents = list(services.agents())

    assert endpoint == {
        "alias": "model-a",
        "api_base": "http://10.0.0.42:8000/v1",
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "generation": 7,
        "node_id": NODE_ID,
        "observed_at": now.isoformat(),
        "plan_digest": DIGEST,
        "state": "published",
    }
    assert agents == [
        {
            "capabilities": ["node.probe"],
            "certificate_expires_at": (now + timedelta(days=30)).isoformat(),
            "last_seen_age_seconds": 0.0,
            "last_seen_at": now.isoformat(),
            "agent_implementation": "python",
            "migration_state": "required",
                "node_id": NODE_ID,
                "protocol_version": 1,
                "platform_version": None,
                "build_digest": None,
                "active_slot": None,
                "agent_sha256": None,
                "supervisor_generation": None,
                "stale": False,
            "state": "active",
        }
    ]
    serialized_agents = json.dumps(agents, sort_keys=True)
    assert "10.0.0.42" not in serialized_agents
    assert "fingerprint-secret" not in serialized_agents
    assert "certificate-body-secret" not in serialized_agents

    projection_client, projection_operator, *_ = _client(operations=services)
    agent_response = projection_client.get(
        "/api/v1/agents", headers=projection_operator
    )
    assert agent_response.status_code == 200
    assert agent_response.json() == {"agents": agents}

    (generation / "litellm.json").unlink()
    endpoint_client, operator, _reconciler, _audits = _client(
        operations=services
    )
    unavailable = endpoint_client.get(
        "/api/v1/endpoints/model-a", headers=operator
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "detail": "endpoint publication unavailable"
    }
    (generation / "litellm.json").write_bytes(litellm_bytes)

    with sessions.begin() as session:
        session.get(RoutePublication, reconciliation_id).state = "publication-pending"
    with pytest.raises(RuntimeError, match="active publication"):
        services.endpoint("model-a")

    with sessions.begin() as session:
        session.get(RoutePublication, reconciliation_id).state = "completed"
    (route_root / "activation.json").write_text("{}")
    with pytest.raises(RuntimeError, match="activation marker"):
        services.endpoint("model-a")


def test_operator_resume_is_rbac_guarded_strict_and_audited() -> None:
    resumed: list[str] = []
    services = OperationApiServices(
        endpoint=lambda _alias: {},
        agents=lambda: (),
        job_operations=lambda _job_id, _cursor, _limit: OperationPage(
            (), None, JobProgress(completed=0, failed=0, running=0, total=0)
        ),
        resume_job=resumed.append,
    )
    client, operator, _reconciler, audits = _client(operations=services)
    job_id = "11111111-1111-4111-8111-111111111111"

    unexpected = client.post(
        f"/api/v1/jobs/{job_id}/resume",
        headers=operator,
        json={"force": True},
    )
    request_id = "33333333-3333-4333-8333-333333333333"
    response = client.post(
        f"/api/v1/jobs/{job_id}/resume",
        headers={**operator, "X-Request-ID": request_id},
    )
    viewer_client, viewer, *_ = _client(operations=services, role="viewer")
    denied = viewer_client.post(f"/api/v1/jobs/{job_id}/resume", headers=viewer)

    assert unexpected.status_code == 422
    assert denied.status_code == 403
    assert response.status_code == 202
    assert response.json() == {"id": job_id, "state": "queued"}
    assert resumed == [job_id]
    assert audits.for_request(request_id).action == "job.resume"


def test_durable_resume_has_one_atomic_winner(tmp_path) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'resume.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    job = Job(
        request_id="33333333-3333-4333-8333-333333333333",
        kind="reconcile",
        state="waiting-for-operator",
        actor="operator",
        base_commit=COMMIT,
        targets=[NODE_ID],
        payload_digest="e" * 64,
        payload={},
        current_attempt=1,
        created_at=now,
        updated_at=now,
    )
    with sessions.begin() as session:
        session.add(job)
    services = operation_api.durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )

    def resume() -> str:
        try:
            services.resume_job(job.id)
            return "won"
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _index: resume(), range(8)))

    assert outcomes.count("won") == 1
    assert outcomes.count("conflict") == 7
    with sessions() as session:
        assert session.get(Job, job.id).state == "queued"


def test_durable_operation_keyset_pages_are_complete_and_aggregated(tmp_path) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'operation-pages.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    job = Job(
        request_id="33333333-3333-4333-8333-333333333333",
        kind="reconcile",
        state="running",
        actor="operator",
        base_commit=COMMIT,
        targets=[NODE_ID],
        payload_digest="e" * 64,
        payload={},
        current_attempt=1,
        created_at=now,
        updated_at=now,
    )
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_ID, state="active", capabilities=[]))
        session.add(job)
        session.flush()
        for index in range(23):
            session.add(
                AgentOperation(
                    parent_job_id=job.id,
                    node_id=NODE_ID,
                    kind="node.probe",
                    payload_digest=f"{index:064x}",
                    payload={},
                    base_commit=COMMIT,
                    state="succeeded" if index < 8 else "queued",
                    current_attempt=0,
                    created_at=now + timedelta(seconds=index // 3),
                    updated_at=now,
                )
            )
    services = operation_api.durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )

    found: list[str] = []
    cursor = None
    while True:
        page = services.job_operations(job.id, cursor, 7)
        found.extend(str(item["id"]) for item in page.items)
        assert page.progress.model_dump() == {
            "completed": 8,
            "failed": 0,
            "running": 15,
            "total": 23,
        }
        cursor = page.next_cursor
        if cursor is None:
            break

    assert len(found) == len(set(found)) == 23


def test_durable_operation_cursor_rejects_cross_job_replay_and_tampering(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'operation-cursor.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    jobs = [
        Job(
            request_id=f"33333333-3333-4333-8333-33333333333{index}",
            kind="reconcile",
            state="running",
            actor="operator",
            base_commit=COMMIT,
            targets=[NODE_ID],
            payload_digest="e" * 64,
            payload={},
            current_attempt=1,
            created_at=now,
            updated_at=now,
        )
        for index in (1, 2)
    ]
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_ID, state="active", capabilities=[]))
        session.add_all(jobs)
        session.flush()
        for job in jobs:
            for index in range(2):
                session.add(
                    AgentOperation(
                        parent_job_id=job.id,
                        node_id=NODE_ID,
                        kind="node.probe",
                        payload_digest=f"{index:064x}",
                        payload={},
                        base_commit=COMMIT,
                        state="queued",
                        current_attempt=0,
                        created_at=now + timedelta(seconds=index),
                        updated_at=now,
                    )
                )
    services = operation_api.durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )
    cursor = services.job_operations(jobs[0].id, None, 1).next_cursor
    assert cursor is not None

    with pytest.raises(ValueError, match="cursor"):
        services.job_operations(jobs[1].id, cursor, 1)
    replacement = "A" if cursor[-1] != "A" else "B"
    with pytest.raises(ValueError, match="cursor"):
        services.job_operations(jobs[0].id, cursor[:-1] + replacement, 1)


def test_target_cursor_rejects_cross_job_and_cross_resource_replay() -> None:
    client, operator, _reconciler, _audits = _client()
    codec = TokenCodec(b"k" * 32).cursor_codec()
    job_id = EnqueuedJob.id
    other_job_cursor = codec.encode(
        resource="job-targets",
        order="index-asc/v1",
        context={"job_id": "99999999-9999-4999-8999-999999999999"},
        boundary=1,
    )
    operation_cursor = codec.encode(
        resource="job-operations",
        order="created-at-asc/id-asc/v1",
        context={"job_id": job_id},
        boundary=[datetime(2026, 8, 5, tzinfo=UTC).isoformat(), "operation"],
    )

    for cursor in (other_job_cursor, operation_cursor):
        response = client.get(
            f"/api/v1/jobs/{job_id}",
            headers=operator,
            params={"target_cursor": cursor, "limit": 1},
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "job cursor is invalid"}




def test_admin_operation_schema_declares_applicable_bounded_errors() -> None:
    services = OperationApiServices(
        endpoint=lambda _alias: {},
        agents=lambda: (),
        job_operations=lambda _job_id, _cursor, _limit: OperationPage(
            (), None, JobProgress(completed=0, failed=0, running=0, total=0)
        ),
        resume_job=lambda _job_id: None,
    )
    client, _operator, _reconciler, _audits = _client(operations=services)
    schema = operation_api.admin_openapi_schema(client.app)
    operations = {
        operation["operationId"]: operation
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"delete", "get", "patch", "post", "put"}
    }
    expected = {
        "applyReconciliation": {"401", "403", "409", "503"},
        "approveAgentEnrollment": {"401", "403", "409", "503"},
        "createEnrollmentGrant": {"401", "403", "503"},
        "getJobLog": {"401", "403", "404", "503"},
        "getPublishedEndpoint": {"401", "404", "503"},
        "planProfileReconciliation": {"401", "403", "409", "503"},
        "planReconciliation": {"401", "403", "409", "503"},
        "rejectAgentEnrollment": {"401", "403", "409", "503"},
        "resumeJob": {"401", "403", "404", "409", "503"},
        "revokeAgentNode": {"401", "403", "404", "503"},
    }
    for operation_id, statuses in expected.items():
        assert statuses <= set(operations[operation_id]["responses"])
        for status_code in statuses:
            response_schema = operations[operation_id]["responses"][status_code][
                "content"
            ]["application/json"]["schema"]
            assert response_schema == {
                "$ref": "#/components/schemas/BoundedErrorResponse"
            }

    error = schema["components"]["schemas"]["BoundedErrorResponse"]
    assert error == {
        "additionalProperties": False,
        "properties": {
            "detail": {
                "maxLength": 256,
                "minLength": 1,
                "title": "Detail",
                "type": "string",
            }
        },
        "required": ["detail"],
        "title": "BoundedErrorResponse",
        "type": "object",
    }

    successes = {
        "approveAgentEnrollment": "EnrollmentDecisionResponse",
        "createEnrollmentGrant": "EnrollmentGrantResponse",
        "listAgentEnrollments": "EnrollmentListResponse",
        "rejectAgentEnrollment": "EnrollmentDecisionResponse",
    }
    for operation_id, component in successes.items():
        success = next(
            response
            for status_code, response in sorted(
                operations[operation_id]["responses"].items()
            )
            if status_code.startswith("2")
        )
        assert success["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{component}"
        }
        assert schema["components"]["schemas"][component][
            "additionalProperties"
        ] is False
