from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from vonk_control import operation_api
from vonk_control.agent_upgrade_status import operator_agent_upgrade_reason
from vonk_control.api import AdminServices, create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.fleet_projection import (
    FleetNodeIdentity,
    FleetSnapshot,
    TelemetryHistoryResponse,
)
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    AgentPresence,
    Base,
    FleetProfile,
    FleetProfileApplication,
    Job,
    Reconciliation,
    RoutePublication,
    RoutePublicationOwner,
)
from vonk_control.operation_api import (
    JobProgress,
    OperationApiServices,
    OperationListPage,
    OperationPage,
    OperationProvider,
    OperationQuery,
    durable_operation_services,
)

COMMIT = "a" * 64
DIGEST = "d" * 64
NODE_ID = "spk_" + "1" * 32


def _encoded(document: object) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


@dataclass
class EnqueuedJob:
    id: str = "11111111-1111-4111-8111-111111111111"
    state: str = "queued"
    kind: str = "reconcile"
    authority_revision: str = COMMIT
    targets: tuple[str, ...] = (NODE_ID,)
    current_attempt: int = 1
    status_reason: str | None = None
    reconciliation_id: str | None = "22222222-2222-4222-8222-222222222222"
    created_at: datetime = datetime(2026, 8, 15, 11, 45, tzinfo=UTC)


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
        return [self.job], None, 1


class Repository:
    def head(self):
        return COMMIT


class ProjectedFleet:
    def __init__(self) -> None:
        self.history_calls: list[tuple[object, ...]] = []
        self.profile_calls: list[tuple[str, str]] = []

    def read(self) -> FleetSnapshot:
        return FleetSnapshot(
            event_cursor=11,
            generated_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            authority_revision=COMMIT,
            nodes=[],
        )

    def telemetry_history(
        self,
        node_id: str,
        *,
        start: datetime,
        end: datetime,
        maximum_points: int,
        resolution: str,
    ) -> TelemetryHistoryResponse:
        self.history_calls.append((node_id, start, end, maximum_points, resolution))
        if node_id != NODE_ID:
            raise KeyError(node_id)
        return TelemetryHistoryResponse(
            node_id=node_id,
            start=start,
            end=end,
            resolution=resolution,
            maximum_points=maximum_points,
            points=[],
        )

    def update_display_name(self, node_id: str, display_name: str) -> FleetNodeIdentity:
        self.profile_calls.append((node_id, display_name))
        if node_id != NODE_ID:
            raise KeyError(node_id)
        return FleetNodeIdentity(
            id=node_id,
            display_name=display_name,
            hostname="spark-3542.internal",
            ip_address="192.168.1.211",
        )


def _client(*, fleet=None, fleet_projection=None, operations=None, role="operator"):
    codec = TokenCodec(b"k" * 32)
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet=fleet or (lambda: {"authority_revision": COMMIT, "nodes": []}),
        fleet_projection=fleet_projection or ProjectedFleet(),
        now=lambda: 10,
        admin=AdminServices(
            authority=Repository(),
            proposals=None,
            changes=None,
        ),
        operations=operations,
    )
    token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
    return (
        TestClient(app),
        {"Authorization": f"Bearer {token}"},
        None,
        audits,
    )


def test_openapi_exposes_only_current_document_contract() -> None:
    client, *_ = _client()

    paths = client.app.openapi()["paths"]

    assert not any(path.startswith("/api/v1/profiles/") for path in paths)
    assert "/api/v1/reconciliations/plan" not in paths
    assert "/api/v1/reconciliations" not in paths
    assert not any(path.startswith("/api/v1/reconciliations/") for path in paths)
    assert not any(path.startswith("/api/v1/updates") for path in paths)
    assert "/api/v1/documents" not in paths


def test_job_activity_summaries_include_their_authoritative_creation_time() -> None:
    client, operator, *_ = _client()

    response = client.get("/api/v1/jobs", headers=operator)

    assert response.status_code == 200
    assert response.json()["jobs"] == [
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "state": "queued",
            "kind": "reconcile",
            "created_at": "2026-08-15T11:45:00Z",
        }
    ]


def test_generic_operation_read_contract_projects_bounded_durable_state() -> None:
    item = {
        "id": "33333333-3333-4333-8333-333333333333",
        "parent_id": "11111111-1111-4111-8111-111111111111",
        "node_ids": [NODE_ID],
        "kind": "recipe-transfer",
        "state": "uncertain",
        "attempt": 2,
        "progress": {
            "phase": "transfer",
            "completed_bytes": 25,
            "total_bytes": None,
            "total_bytes_known": False,
            "members": [
                {
                    "member_id": NODE_ID,
                    "phase": "transfer",
                    "completed_bytes": 25,
                    "state": "uncertain",
                }
            ],
        },
        "result": {
            "uncertain": True,
            "failure": {
                "error_code": "member_lost",
                "summary": "member did not report completion",
            },
        },
        "supported_actions": ["retry"],
        "created_at": "2026-08-15T11:59:00Z",
        "updated_at": "2026-08-15T12:00:00Z",
    }
    services = OperationApiServices(
        endpoint=lambda _alias: {},
        agents=lambda: (),
        job_operations=lambda _job_id, _cursor, _limit: OperationPage(
            (), None, JobProgress(completed=0, failed=0, running=0, total=0)
        ),
        resume_job=lambda _job_id: None,
        list_operations=lambda _cursor, _limit, _state, _node_id: (
            operation_api.OperationListPage((item,), None, 1)
        ),
        get_operation=lambda _operation_id: item,
    )
    client, operator, *_ = _client(operations=services)

    listed = client.get(
        "/api/v1/operations",
        headers=operator,
        params={"state": "uncertain", "node_id": NODE_ID},
    )
    detail = client.get(f"/api/v1/operations/{item['id']}", headers=operator)

    assert listed.status_code == 200
    assert listed.json()["schema_version"] == 2
    assert listed.json()["total"] == 1
    assert listed.json()["operations"][0]["parent_id"] == item["parent_id"]
    assert listed.json()["operations"][0]["node_ids"] == [NODE_ID]
    assert listed.json()["operations"][0]["schema_version"] == 2
    assert listed.json()["operations"][0]["recovery"] == {
        "uncertain": True,
        "actions": ["inspect"],
        "explanation": "Inspect the durable outcome before taking recovery action.",
    }
    assert listed.json()["operations"][0]["progress"]["total_bytes_known"] is False
    assert detail.status_code == 200
    assert detail.json() == listed.json()["operations"][0]


def test_generic_operation_read_contract_is_unavailable_without_projection() -> None:
    client, operator, *_ = _client()

    assert client.get("/api/v1/operations", headers=operator).status_code == 503
    assert (
        client.get(
            "/api/v1/operations/33333333-3333-4333-8333-333333333333",
            headers=operator,
        ).status_code
        == 503
    )


def test_global_operation_projection_merges_typed_provider_families() -> None:
    rows = {
        "cache-1": {
            "id": "cache-1",
            "parent_id": "job-cache",
            "node_ids": [],
            "kind": "cache-download",
            "state": "running",
            "attempt": 1,
            "progress": {"phase": "download", "total_unknown": True},
            "updated_at": "2026-08-15T12:01:00Z",
            "created_at": "2026-08-15T12:01:00Z",
            "supported_actions": [],
        },
        "run-1": {
            "id": "run-1",
            "parent_id": "job-run",
            "node_ids": [NODE_ID, "spk_" + "2" * 32],
            "kind": "run",
            "state": "succeeded",
            "attempt": 1,
            "progress": {"phase": "final_verify"},
            "updated_at": "2026-08-15T12:00:00Z",
            "created_at": "2026-08-15T12:00:00Z",
            "supported_actions": [],
        },
    }

    def provider_for(ids: tuple[str, ...]) -> OperationProvider:
        def list_rows(query: OperationQuery) -> OperationListPage:
            selected = [rows[row_id] for row_id in ids]
            if query.node_id is not None:
                selected = [
                    row for row in selected if query.node_id in row["node_ids"]
                ]
            total = len(selected)
            if query.after is not None:
                selected = [
                    row
                    for row in selected
                    if operation_api._operation_boundary(row) < query.after
                ]
            selected.sort(key=operation_api._operation_boundary, reverse=True)
            return OperationListPage(selected[: query.limit], None, total)

        def get_row(operation_id: str) -> dict[str, object]:
            for row_id in ids:
                if row_id == operation_id:
                    return rows[row_id]
            raise KeyError(operation_id)

        return OperationProvider(
            family=ids[0].split("-", 1)[0],
            list_operations=list_rows,
            get_operation=get_row,
        )

    services = OperationApiServices(
        endpoint=lambda _alias: {},
        agents=lambda: (),
        job_operations=lambda _job_id, _cursor, _limit: OperationPage(
            (), None, JobProgress(completed=0, failed=0, running=0, total=0)
        ),
        resume_job=lambda _job_id: None,
        operation_providers=(provider_for(("cache-1",)), provider_for(("run-1",))),
        cursor_codec=TokenCodec(b"p" * 32).cursor_codec(),
    )
    client, operator, *_ = _client(operations=services)

    first = client.get("/api/v1/operations", headers=operator, params={"limit": "1"})
    second = client.get(
        "/api/v1/operations",
        headers=operator,
        params={"limit": "1", "cursor": first.json()["next_cursor"]},
    )
    detail = client.get("/api/v1/operations/run-1", headers=operator)
    filtered = client.get(
        "/api/v1/operations",
        headers=operator,
        params={"node_id": "spk_" + "2" * 32},
    )

    assert first.status_code == 200
    assert first.json()["schema_version"] == 2
    assert first.json()["total"] == 2
    assert first.json()["operations"][0]["id"] == "cache-1"
    assert first.json()["operations"][0]["node_ids"] == []
    assert second.status_code == 200
    assert second.json()["operations"][0]["id"] == "run-1"
    assert second.json()["operations"][0]["node_ids"] == [
        NODE_ID,
        "spk_" + "2" * 32,
    ]
    assert detail.status_code == 200
    assert detail.json()["kind"] == "run"
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["operations"][0]["id"] == "run-1"


def test_profile_operation_provider_is_registered_through_the_global_api(
    tmp_path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'profile-operations.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    profile_id = "44444444-4444-4444-8444-444444444444"
    newest_id = "55555555-5555-4555-8555-555555555555"
    older_id = "66666666-6666-4666-8666-666666666666"
    second_node = "spk_" + "2" * 32
    now = datetime(2026, 8, 15, 12, tzinfo=UTC)
    with sessions.begin() as session:
        session.add(
            FleetProfile(
                id=profile_id,
                name="Studio",
                description="",
                installation_policy="keep-cached",
                assignments=[],
                scope=[NODE_ID, second_node],
                labels={},
                favorite=False,
                created_by="admin",
                created_at=now,
                updated_at=now,
            )
        )
        for operation_id, request_key, plan_digest, created_at, node_ids in (
            (
                newest_id,
                "77777777-7777-4777-8777-777777777777",
                "7" * 64,
                now,
                [NODE_ID, second_node],
            ),
            (
                older_id,
                "88888888-8888-4888-8888-888888888888",
                "8" * 64,
                now - timedelta(minutes=1),
                [NODE_ID],
            ),
        ):
            session.add(
                FleetProfileApplication(
                    id=operation_id,
                    request_key=request_key,
                    profile_id=profile_id,
                    profile_digest="9" * 64,
                    plan_digest=plan_digest,
                    state="running",
                    plan={"scope": {"node_ids": node_ids}, "steps": [{"kind": "start"}]},
                    current_step=0,
                    current_operation_id=None,
                    progress={"operation_kind": "fleet-profile.apply"},
                    result=None,
                    status_reason=None,
                    actor="admin",
                    created_at=created_at,
                    updated_at=created_at,
                )
            )

    profiles = FleetProfileService(sessions, clock=lambda: now)
    services = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=TokenCodec(b"q" * 32).cursor_codec(),
        operation_providers=(profiles.operation_provider(),),
    )
    client, operator, *_ = _client(operations=services)

    first = client.get(
        "/api/v1/operations", headers=operator, params={"limit": 1}
    )
    detail = client.get(f"/api/v1/operations/{newest_id}", headers=operator)
    second = client.get(
        "/api/v1/operations",
        headers=operator,
        params={"limit": 1, "cursor": first.json()["next_cursor"]},
    )
    filtered = client.get(
        "/api/v1/operations",
        headers=operator,
        params={"node_id": second_node},
    )

    assert first.status_code == 200
    assert first.json()["total"] == 2
    assert first.json()["operations"][0]["id"] == newest_id
    assert first.json()["operations"][0]["node_ids"] == [NODE_ID, second_node]
    assert first.json()["next_cursor"] is not None
    assert detail.status_code == 200
    assert detail.json()["kind"] == "fleet-profile.apply"
    assert detail.json()["progress"] == {"phase": "start"}
    assert second.status_code == 200
    assert second.json()["total"] == 2
    assert second.json()["operations"][0]["id"] == older_id
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["operations"][0]["id"] == newest_id


def test_fleet_exposes_visual_state_and_node_evidence() -> None:
    client, operator, *_ = _client()

    visual = client.get("/api/v1/fleet", headers=operator)
    evidence = client.get("/api/v1/nodes/status", headers=operator)

    assert visual.status_code == 200
    assert visual.json() == {
        "schema_version": 1,
        "event_cursor": 11,
        "generated_at": "2026-08-15T12:00:00Z",
        "authority_revision": COMMIT,
        "nodes": [],
    }
    assert "evidence_digest" not in visual.json()
    assert evidence.status_code == 200
    assert evidence.json()["authority_revision"] == COMMIT
    assert evidence.json()["nodes"] == []
    assert len(evidence.json()["evidence_digest"]) == 64
    assert "event_cursor" not in evidence.json()


def test_node_telemetry_history_is_typed_authorized_and_capped() -> None:
    projection = ProjectedFleet()
    client, operator, *_ = _client(fleet_projection=projection)
    params = {
        "start": "2026-08-15T11:00:00Z",
        "end": "2026-08-15T12:00:00Z",
        "maximum_points": "1500",
        "resolution": "raw",
    }

    response = client.get(
        f"/api/v1/nodes/{NODE_ID}/telemetry",
        headers=operator,
        params=params,
    )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "node_id": NODE_ID,
        "start": "2026-08-15T11:00:00Z",
        "end": "2026-08-15T12:00:00Z",
        "resolution": "raw",
        "maximum_points": 1500,
        "points": [],
    }
    assert projection.history_calls == [
        (
            NODE_ID,
            datetime(2026, 8, 15, 11, tzinfo=UTC),
            datetime(2026, 8, 15, 12, tzinfo=UTC),
            1500,
            "raw",
        )
    ]
    assert (
        client.get(
            f"/api/v1/nodes/{NODE_ID}/telemetry",
            headers=operator,
            params={key: value for key, value in params.items() if key != "resolution"},
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/nodes/{NODE_ID}/telemetry",
            headers=operator,
            params={**params, "maximum_points": "3001"},
        ).status_code
        == 422
    )
    assert len(projection.history_calls) == 1
    assert (
        client.get(
            f"/api/v1/nodes/{'spk_' + 'f' * 32}/telemetry",
            headers=operator,
            params=params,
        ).status_code
        == 404
    )


def test_operator_can_rename_node_without_mutating_technical_identity() -> None:
    projection = ProjectedFleet()
    client, operator, _, audits = _client(fleet_projection=projection)

    response = client.patch(
        f"/api/v1/nodes/{NODE_ID}/profile",
        headers=operator,
        json={"display_name": "  Studio Spark  "},
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": NODE_ID,
        "display_name": "Studio Spark",
        "hostname": "spark-3542.internal",
        "ip_address": "192.168.1.211",
    }
    assert projection.profile_calls == [(NODE_ID, "Studio Spark")]
    assert audits.list()[0].action == "fleet.node.rename"
    assert audits.list()[0].targets == (NODE_ID,)


def test_node_rename_maps_database_failures_to_bounded_unavailability() -> None:
    class UnavailableFleet(ProjectedFleet):
        def update_display_name(
            self, node_id: str, display_name: str
        ) -> FleetNodeIdentity:
            del node_id, display_name
            raise SQLAlchemyError("database detail must not cross the API boundary")

    client, operator, *_ = _client(fleet_projection=UnavailableFleet())

    response = client.patch(
        f"/api/v1/nodes/{NODE_ID}/profile",
        headers=operator,
        json={"display_name": "Studio Spark"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Fleet profile update unavailable"}


@pytest.mark.parametrize(
    "display_name",
    ["", "   ", "bad\nname", "x" * 81],
)
def test_node_rename_rejects_invalid_friendly_names(display_name: str) -> None:
    projection = ProjectedFleet()
    client, operator, *_ = _client(fleet_projection=projection)

    response = client.patch(
        f"/api/v1/nodes/{NODE_ID}/profile",
        headers=operator,
        json={"display_name": display_name},
    )

    assert response.status_code == 422
    assert projection.profile_calls == []


def test_viewer_cannot_rename_node() -> None:
    projection = ProjectedFleet()
    client, viewer, *_ = _client(fleet_projection=projection, role="viewer")

    response = client.patch(
        f"/api/v1/nodes/{NODE_ID}/profile",
        headers=viewer,
        json={"display_name": "Studio Spark"},
    )

    assert response.status_code == 403
    assert projection.profile_calls == []


def test_nodes_status_marks_missing_observation_unknown_and_stale() -> None:
    def fleet():
        return {
            "authority_revision": COMMIT,
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
                    "health_probe_stale": True,
                    "stale": True,
                }
            ],
        }

    client, operator, _reconciler, _audits = _client(fleet=fleet)

    response = client.get("/api/v1/nodes/status", headers=operator)

    assert response.status_code == 200
    node = response.json()["nodes"][0]
    assert node["healthy"] is None
    assert node["health_probe_stale"] is True
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
        "agent_upgrade_diagnostics": None,
        "authority_revision": COMMIT,
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
                authority_revision=COMMIT,
                status="succeeded",
                summary={},
                graph={
                    "authority_revision": COMMIT,
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
                protocol_version=3,
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
            "node_id": NODE_ID,
            "protocol_version": 3,
            "semantic_version": None,
            "build_digest": None,
            "binary_digest": None,
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
    endpoint_client, operator, _reconciler, _audits = _client(operations=services)
    unavailable = endpoint_client.get("/api/v1/endpoints/model-a", headers=operator)
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "endpoint publication unavailable"}
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
        authority_revision=COMMIT,
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


def test_durable_resume_dispatches_agent_upgrade_to_its_operation_queue(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade-resume.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    job = Job(
        request_id="33333333-3333-4333-8333-333333333333",
        kind="agent-upgrade",
        state="waiting-for-operator",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_ID],
        payload_digest="e" * 64,
        payload={"immutable": "upgrade-plan"},
        current_attempt=0,
        created_at=now,
        updated_at=now,
    )
    with sessions.begin() as session:
        session.add(job)
    resumed: list[str] = []
    services = operation_api.durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
        resume_agent_upgrade=resumed.append,
    )

    services.resume_job(job.id)

    assert resumed == [job.id]
    with sessions() as session:
        stored = session.get(Job, job.id)
        assert stored is not None
        assert stored.state == "waiting-for-operator"
        assert stored.payload == {"immutable": "upgrade-plan"}
        assert stored.current_attempt == 0


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
        authority_revision=COMMIT,
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
                    authority_revision=COMMIT,
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


def test_agent_upgrade_projection_keeps_raw_reason_and_exact_identity_evidence(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 28, 21, 23, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade-diagnostics.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    expected_binary = "b" * 64
    expected_build = "sha256:" + "c" * 64
    old_binary = "d" * 64
    old_build = "sha256:" + "e" * 64
    package = {
        "architecture": "linux-arm64",
        "package_bytes": 5_539_780,
        "package_sha256": "f" * 64,
        "package_signature": "1" * 128,
        "package_url": "https://install.vonkforge.ai/example.deb",
        "package_version": "0.1.0~dev.350+g15f9faf7c5bf",
        "schema_version": 1,
        "target_binary_digest": expected_binary,
        "target_build_digest": expected_build,
    }
    job = Job(
        request_id="33333333-3333-4333-8333-333333333333",
        kind="agent-upgrade",
        state="waiting-for-operator",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_ID],
        payload_digest="a" * 64,
        payload={
            "node_order": [NODE_ID],
            "package": package,
            "strategy": "one-at-a-time",
        },
        current_attempt=1,
        status_reason="operator-facing explanation",
        created_at=now,
        updated_at=now,
    )
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_ID,
                state="active",
                capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
                semantic_version="0.1.0",
                binary_digest=old_binary,
                build_digest=old_build,
            )
        )
        session.add(
            AgentCertificate(
                serial="serial-upgrade",
                node_id=NODE_ID,
                not_before=now - timedelta(days=1),
                not_after=now + timedelta(days=1),
                fingerprint="fingerprint-upgrade",
            )
        )
        session.add(job)
        session.flush()
        operation = AgentOperation(
            parent_job_id=job.id,
            node_id=NODE_ID,
            kind="agent.upgrade.v1",
            payload_digest="f" * 64,
            payload=package,
            authority_revision=COMMIT,
            state="waiting-for-operator",
            current_attempt=2,
            created_at=now,
            updated_at=now,
        )
        session.add(operation)
        session.flush()
        session.add(
            AgentOperationAttempt(
                operation_id=operation.id,
                attempt=2,
                fence="44444444-4444-4444-8444-444444444444",
                lease_deadline=now,
                agent_certificate_serial="serial-upgrade",
                state="failed",
                result={"reason": "agent upgrade request is invalid"},
            )
        )
    services = operation_api.durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )

    page = services.job_operations(job.id, None, 20)
    diagnostics = page.agent_upgrade_diagnostics

    assert diagnostics == {
        "expected_identity": {
            "version": "0.1.0~dev.350+g15f9faf7c5bf",
            "binary_digest": expected_binary,
            "build_digest": expected_build,
        },
        "targets": [
            {
                "node_id": NODE_ID,
                "state": "waiting-for-operator",
                "attempts": 2,
                "target_proven": False,
                "observed_identity": {
                    "version": "0.1.0",
                    "binary_digest": old_binary,
                    "build_digest": old_build,
                },
                "raw_reason": "agent upgrade request is invalid",
                "retry_not_before": None,
                "retry_queued": False,
            }
        ],
        "legacy_generic_ambiguous": True,
        "next_action": (
            "Keep the rollout paused and inspect the Spark package-helper and dpkg "
            "recovery state before resuming. When ready, Resume queues the retry "
            "behind a new safety delay; it does not dispatch immediately. Do not "
            "advance to another Spark until this Spark reports the exact target "
            "identity."
        ),
        "operator_summary": operator_agent_upgrade_reason(
            node_id=NODE_ID,
            attempt_count=2,
            package=package,
            observed_semantic_version="0.1.0",
            observed_binary_digest=old_binary,
            observed_build_digest=old_build,
            raw_reason="agent upgrade request is invalid",
            retry_queued=False,
        ),
    }
    projected = operation_api.job_response(
        job,
        page,
        target_cursor=0,
        limit=20,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )
    assert projected.status_reason == diagnostics["operator_summary"]
    assert projected.agent_upgrade_diagnostics is not None
    assert projected.agent_upgrade_diagnostics.targets[0].raw_reason == (
        "agent upgrade request is invalid"
    )

    with sessions.begin() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        assert operation is not None
        operation.retry_disposition = "retry"
        operation.retry_disposition_attempt = operation.current_attempt
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        assert attempt is not None
        attempt.lease_deadline = now + timedelta(seconds=240)

    queued = services.job_operations(job.id, None, 20).agent_upgrade_diagnostics
    assert queued is not None
    assert queued["targets"][0]["retry_queued"] is True
    assert (
        queued["targets"][0]["retry_not_before"]
        == (now + timedelta(seconds=240)).isoformat()
    )
    assert queued["next_action"] is not None
    assert "controller-managed retry" in queued["next_action"]

    with sessions.begin() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        node = session.get(AgentNode, NODE_ID)
        assert operation is not None and node is not None
        # Matching the two headline digests is deliberately weaker than the
        # controller's accepted success gate.
        node.binary_digest = expected_binary
        node.build_digest = expected_build
        operation.retry_disposition = None
        operation.retry_disposition_attempt = None

    matching_digests = services.job_operations(
        job.id, None, 20
    ).agent_upgrade_diagnostics
    assert matching_digests is not None
    assert matching_digests["targets"][0]["target_proven"] is False
    assert matching_digests["targets"][0]["retry_not_before"] is None

    with sessions.begin() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        assert operation is not None and attempt is not None
        attempt.result = {"reason": "agent upgrade helper is unavailable"}

    specific = services.job_operations(job.id, None, 20).agent_upgrade_diagnostics
    assert specific is not None
    assert specific["legacy_generic_ambiguous"] is False
    assert specific["next_action"] is not None
    assert (
        "Resume queues the retry behind a new safety delay" in specific["next_action"]
    )


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
            authority_revision=COMMIT,
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
                        authority_revision=COMMIT,
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
        "createEnrollmentGrant": {"401", "403", "503"},
        "getJobLog": {"401", "403", "404", "503"},
        "getPublishedEndpoint": {"401", "404", "503"},
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
        "createEnrollmentGrant": "EnrollmentGrantResponse",
        "listAgentEnrollments": "EnrollmentListResponse",
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
        assert (
            schema["components"]["schemas"][component]["additionalProperties"] is False
        )


def test_fleet_operation_registry_keeps_visual_and_evidence_contracts_distinct() -> (
    None
):
    client, _operator, _reconciler, _audits = _client()

    schema = operation_api.admin_openapi_schema(client.app)
    paths = schema["paths"]

    assert paths["/api/v1/fleet"]["get"]["operationId"] == "getFleetStatus"
    assert paths["/api/v1/fleet"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/FleetSnapshot"}
    assert paths["/api/v1/nodes/status"]["get"]["operationId"] == ("getNodeStatuses")
    assert paths["/api/v1/nodes/status"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/FleetStatusResponse"}
    node_status = schema["components"]["schemas"]["NodeStatus"]
    health_probe_stale = node_status["properties"]["health_probe_stale"]
    legacy_stale = node_status["properties"]["stale"]
    assert "not aggregate node readiness" in health_probe_stale["description"]
    assert legacy_stale["deprecated"] is True
    assert "health_probe_stale" in legacy_stale["description"]
    assert paths["/api/v1/nodes/status"]["get"]["summary"] == (
        "Read explicit node health-probe evidence"
    )
    assert paths["/api/v1/nodes/{node_id}/telemetry"]["get"]["operationId"] == (
        "getNodeTelemetryHistory"
    )
    assert paths["/api/v1/fleet/stream"]["get"]["operationId"] == ("streamFleetEvents")
    assert paths["/api/v1/fleet/stream"]["get"]["parameters"] == [
        {
            "description": (
                "Optional durable Fleet cursor; duplicate and numeric validity "
                "are checked from the raw header list."
            ),
            "in": "header",
            "name": "Last-Event-ID",
            "required": False,
            "schema": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": (
                    "Optional durable Fleet cursor; duplicate and numeric validity "
                    "are checked from the raw header list."
                ),
                "title": "Last-Event-Id",
            },
        }
    ]
    assert paths["/api/v1/fleet/stream"]["get"]["security"] == [{"BrowserSession": []}]
    assert paths["/api/v1/fleet"]["get"]["security"] == [{"BearerAuth": []}]
    assert paths["/api/v1/nodes/status"]["get"]["security"] == [{"BearerAuth": []}]


def test_recipe_action_preview_registry_is_explicit_and_strict() -> None:
    client, _operator, _reconciler, _audits = _client()

    schema = operation_api.admin_openapi_schema(client.app)
    paths = schema["paths"]

    assert paths["/api/v1/recipes/stop-plans/preview"]["post"]["operationId"] == (
        "previewRecipeStop"
    )
    assert (
        paths["/api/v1/recipes/uninstall-plans/preview"]["post"]["operationId"]
        == "previewRecipeUninstall"
    )
    assert paths["/api/v1/recipes/runs/{run_id}/stop"]["post"]["operationId"] == (
        "stopRecipeRun"
    )
    assert (
        paths["/api/v1/recipes/installations/{installation_id}/uninstall"]["post"][
            "operationId"
        ]
        == "uninstallRecipe"
    )
    for component in (
        "StopPreviewRequest",
        "StopRequest",
        "UninstallPreviewRequest",
        "UninstallRequest",
        "StopPlanResponse",
        "UninstallPlanResponse",
    ):
        assert (
            schema["components"]["schemas"][component]["additionalProperties"] is False
        )
