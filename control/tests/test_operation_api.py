from __future__ import annotations

import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control import operation_api
from vonk_control.agent_upgrade_status import operator_agent_upgrade_reason
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.bounded_json import BoundedJSONError
from vonk_control.fleet_profile_contract import (
    FleetProfilePlanStep,
    FleetProfilePlanSummary,
    FleetProfilePreview,
    FleetProfileScopePreview,
)
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.fleet_projection import FleetSnapshot
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
        Base,
        FleetProfile,
        FleetProfileApplication,
        Job,
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
from vonk_control.strict_json import serialize_json_value

COMMIT = "a" * 64
DIGEST = "d" * 64
NODE_ID = "spk_" + "1" * 32


def _profile_operation_plan(
    *, profile_id: str, profile_digest: str, plan_digest: str, node_ids: list[str], now: datetime
) -> dict[str, object]:
    """Build the same complete preview document persisted by profile apply."""

    return FleetProfilePreview(
        profile_id=profile_id,
        profile_name="Studio",
        profile_digest=profile_digest,
        generated_at=now,
        allowed=True,
        scope=FleetProfileScopePreview(node_ids=node_ids, idle_node_ids=node_ids),
        summary=FleetProfilePlanSummary(
            already_correct=0,
            placements=0,
            builds=0,
            distributions=0,
            installs=0,
            starts=1,
            stops=0,
            uninstalls=0,
            blockers=0,
        ),
        assignments=[],
        preparations=[],
        steps=[
            FleetProfilePlanStep(
                index=0, kind="start", node_ids=node_ids, label="Start profile"
            )
        ],
        reasons=[],
        plan_digest=plan_digest,
    ).model_dump(mode="json")


def _encoded(document: object) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _first_diagnostic_target(diagnostics: Mapping[str, object]) -> Mapping[str, object]:
    """Return the first projected agent-upgrade target or fail loudly."""

    targets = diagnostics["targets"]
    assert isinstance(targets, list) and targets
    target = targets[0]
    assert isinstance(target, dict)
    return target


@dataclass
class EnqueuedJob:
    id: str = "11111111-1111-4111-8111-111111111111"
    state: str = "queued"
    kind: str = "reconcile"
    authority_revision: str = COMMIT
    targets: tuple[str, ...] = (NODE_ID,)
    current_attempt: int = 1
    status_reason: str | None = None
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


class ProjectedFleet:
    def read(self) -> FleetSnapshot:
        return FleetSnapshot(
            event_cursor=11,
            generated_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            authority_revision=COMMIT,
            nodes=[],
        )

def _client(*, fleet_projection=None, operations=None, role="operator"):
    codec = TokenCodec(b"k" * 32)
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet_projection=fleet_projection or ProjectedFleet(),
        now=lambda: 10,
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

    application = client.app
    assert isinstance(application, FastAPI)
    paths = application.openapi()["paths"]

    assert not any(path.startswith("/api/profiles/") for path in paths)
    assert "/api/reconciliations/plan" not in paths
    assert "/api/reconciliations" not in paths
    assert not any(path.startswith("/api/reconciliations/") for path in paths)
    assert not any(path.startswith("/api/updates") for path in paths)
    assert "/api/documents" not in paths


def test_job_activity_summaries_include_their_authoritative_creation_time() -> None:
    client, operator, *_ = _client()

    response = client.get("/api/jobs", headers=operator)

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
        "/api/operations",
        headers=operator,
        params={"state": "uncertain", "node_id": NODE_ID},
    )
    detail = client.get(f"/api/operations/{item['id']}", headers=operator)

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


def test_progress_projection_accepts_phase_only_bytes_and_object_identity() -> None:
    projected = operation_api._progress_projection(
        {
            "phase": "download",
            "kind": "oci-layer",
            "object_sha256": "a" * 64,
            "completed_bytes": 128,
            "total_bytes": 256,
            "total_bytes_known": True,
        }
    )
    assert projected is not None
    assert serialize_json_value(projected) == {
        "phase": "download",
        "activity": "waiting",
        "kind": "oci-layer",
        "object_sha256": "a" * 64,
        "completed_bytes": 128,
        "total_bytes": 256,
        "total_bytes_known": True,
        "members": [],
    }
    assert serialize_json_value(operation_api._progress_projection({"phase": "verify"})) == {
        "phase": "verify",
        "activity": "waiting",
        "completed_bytes": 0,
        "total_bytes_known": False,
        "members": [],
    }
    for retired in ("bytes_done", "bytes_completed", "bytes_total", "rate"):
        with pytest.raises(ValueError):
            operation_api._progress_projection({"phase": "download", retired: 1})


def test_generic_operation_read_contract_is_unavailable_without_projection() -> None:
    client, operator, *_ = _client()

    assert client.get("/api/operations", headers=operator).status_code == 503
    assert (
        client.get(
            "/api/operations/33333333-3333-4333-8333-333333333333",
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
            "progress": {
                "phase": "download",
                "total_bytes": None,
                "total_bytes_known": False,
            },
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
                selected = [row for row in selected if query.node_id in row["node_ids"]]
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

    first = client.get("/api/operations", headers=operator, params={"limit": "1"})
    second = client.get(
        "/api/operations",
        headers=operator,
        params={"limit": "1", "cursor": first.json()["next_cursor"]},
    )
    detail = client.get("/api/operations/run-1", headers=operator)
    filtered = client.get(
        "/api/operations",
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


@pytest.mark.parametrize("profile_state", ["running", "failed", "waiting-for-operator"])
def test_profile_operation_provider_is_registered_through_the_global_api(
    tmp_path, profile_state,
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
                    number=1,
                    name="Studio",
                description="",
                installation_policy="keep-cached",
                assignments=[],
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
                    state=profile_state,
                    plan=_profile_operation_plan(
                        profile_id=profile_id,
                        profile_digest="9" * 64,
                        plan_digest=plan_digest,
                        node_ids=node_ids,
                        now=created_at,
                    ),
                    current_step=0,
                    current_operation_id=None,
                    progress={
                        "operation_kind": "fleet-profile.apply",
                        "completed_steps": 0,
                        "total_steps": 1,
                    },
                    result={"changed": False, "completed_steps": 0},
                    status_reason=(
                        "Spark could not start: token=private-credential"
                        if profile_state != "running" else None
                    ),
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

    first = client.get("/api/operations", headers=operator, params={"limit": 1})
    detail = client.get(f"/api/operations/{newest_id}", headers=operator)
    second = client.get(
        "/api/operations",
        headers=operator,
        params={"limit": 1, "cursor": first.json()["next_cursor"]},
    )
    filtered = client.get(
        "/api/operations",
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
    expected_progress = {
        "phase": "start",
        "completed_bytes": 0,
        "total_bytes_known": False,
        "members": [],
    }
    if profile_state == "running":
        expected_progress["activity"] = "waiting"
    assert detail.json()["progress"] == expected_progress
    if profile_state != "running":
        failure = detail.json()["failure"]
        assert failure["error_code"] == "fleet_profile_application_failed"
        assert failure["detail"] == "Spark could not start: token=<redacted>"
        assert failure["uncertain"] is (profile_state == "waiting-for-operator")
        assert first.json()["operations"][0]["failure"] == failure
        assert detail.json()["recovery"]["uncertain"] == failure["uncertain"]
        assert "private-credential" not in first.text + detail.text
    else:
        assert "failure" not in detail.json()
    assert second.status_code == 200
    assert second.json()["total"] == 2
    assert second.json()["operations"][0]["id"] == older_id
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["operations"][0]["id"] == newest_id


def test_fleet_exposes_typed_visual_state() -> None:
    client, operator, *_ = _client()

    visual = client.get("/api/fleet", headers=operator)

    assert visual.status_code == 200
    assert visual.json() == {
        "schema_version": 1,
        "event_cursor": 11,
        "generated_at": "2026-08-15T12:00:00Z",
        "authority_revision": COMMIT,
        "nodes": [],
    }
    assert "evidence_digest" not in visual.json()


def test_job_status_has_typed_progress_fields_without_payloads() -> None:
    client, operator, _reconciler, _audits = _client()

    response = client.get(
        "/api/jobs/11111111-1111-4111-8111-111111111111",
        headers=operator,
    )

    assert response.status_code == 200
    assert response.json() == {
        "authority_revision": COMMIT,
        "current_attempt": 1,
        "id": "11111111-1111-4111-8111-111111111111",
        "kind": "reconcile",
        "operations": [],
        "operation_total": 0,
        "progress": {"completed": 0, "failed": 0, "running": 0, "total": 0},
        "state": "queued",
        "targets": [NODE_ID],
        "target_total": 1,
    }
    encoded = json.dumps(response.json(), sort_keys=True)
    assert "payload" not in encoded
    assert "result" not in encoded




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
        f"/api/jobs/{job_id}/resume",
        headers=operator,
        json={"force": True},
    )
    request_id = "33333333-3333-4333-8333-333333333333"
    response = client.post(
        f"/api/jobs/{job_id}/resume",
        headers={**operator, "X-Request-ID": request_id},
    )
    viewer_client, viewer, *_ = _client(operations=services, role="viewer")
    denied = viewer_client.post(f"/api/jobs/{job_id}/resume", headers=viewer)

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
        stored_job = session.get(Job, job.id)
        assert stored_job is not None
        assert stored_job.state == "queued"


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
        assert page.progress.operation is not None
        assert len(page.progress.operation.members) == 23
        assert page.progress.operation.total_bytes is None
        assert page.progress.model_dump(exclude={"operation"}) == {
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
    assert diagnostics is not None

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
        "failure_details_unavailable": True,
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
    queued_target = _first_diagnostic_target(queued)
    assert queued_target["retry_queued"] is True
    assert (
        queued_target["retry_not_before"]
        == (now + timedelta(seconds=240)).isoformat()
    )
    queued_next_action = queued["next_action"]
    assert queued_next_action is not None
    assert isinstance(queued_next_action, str)
    assert "controller-managed retry" in queued_next_action

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
    matching_target = _first_diagnostic_target(matching_digests)
    assert matching_target["target_proven"] is False
    assert matching_target["retry_not_before"] is None

    with sessions.begin() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
        )
        assert operation is not None
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        assert attempt is not None
        attempt.result = {"reason": "agent upgrade helper is unavailable"}

    specific = services.job_operations(job.id, None, 20).agent_upgrade_diagnostics
    assert specific is not None
    assert specific["failure_details_unavailable"] is False
    specific_next_action = specific["next_action"]
    assert specific_next_action is not None
    assert isinstance(specific_next_action, str)
    assert (
        "Resume queues the retry behind a new safety delay" in specific_next_action
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
            f"/api/jobs/{job_id}",
            headers=operator,
            params={"target_cursor": cursor, "limit": 1},
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "job cursor is invalid", "issues": []}


def test_stored_operation_state_failure_is_not_reported_as_a_cursor_fault() -> None:
    """Unreadable durable state is the Controller's fault, not the caller's.

    Both projections surface a stored document that no longer validates as a
    ``ValueError``, and the routes answered that with 422 "cursor is invalid",
    blaming the request for a cursor the caller never sent.
    """
    def unavailable(*_args: object) -> NoReturn:
        raise BoundedJSONError("stored operation progress is invalid")

    services = OperationApiServices(
        endpoint=lambda _alias: {},
        agents=lambda: (),
        job_operations=unavailable,
        resume_job=lambda _job_id: None,
        list_operations=unavailable,
        get_operation=lambda _operation_id: {},
    )
    client, operator, *_ = _client(operations=services)

    detail = client.get(f"/api/jobs/{EnqueuedJob.id}", headers=operator)
    assert detail.status_code == 503
    assert detail.json()["detail"] == "operation projection unavailable"

    listed = client.get("/api/operations", headers=operator)
    assert listed.status_code == 503
    assert listed.json()["detail"] == "operation projection unavailable"








def test_parallel_job_byte_aggregate_is_independent_of_operation_page(tmp_path) -> None:
    now = datetime.now(UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'parallel-progress.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    job = Job(request_id="33333333-3333-4333-8333-333333333333", kind="reconcile", state="running",
              actor="operator", authority_revision=COMMIT, targets=[NODE_ID, "spk_"+"2"*32],
              payload_digest="e"*64, payload={}, current_attempt=1, created_at=now, updated_at=now)
    with sessions.begin() as session:
        session.add(job)
        session.flush()
        for index, target in enumerate(job.targets, 1):
            session.add(AgentNode(node_id=target, state="active", capabilities=[]))
            operation = AgentOperation(parent_job_id=job.id, node_id=target, kind="node.probe",
                                       payload_digest=f"{index:064x}", payload={}, authority_revision=COMMIT,
                                       state="running", current_attempt=1, created_at=now, updated_at=now)
            session.add(operation)
            session.flush()
            session.add(AgentOperationAttempt(operation_id=operation.id, attempt=1, fence=f"00000000-0000-4000-8000-{index:012d}",
                lease_deadline=now+timedelta(minutes=1), agent_certificate_serial=f"serial-{index}", state="running",
                progress={"phase":"copying", "completed_bytes":index*10, "total_bytes":index*100,
                          "total_bytes_known":True, "bytes_per_second":float(index*10), "eta_seconds":9.0,
                          "observed_at":now.isoformat(), "last_progress_at":now.isoformat()}))
    services = operation_api.durable_operation_services(sessions, tmp_path / "routes", clock=lambda: now,
                                                        cursors=TokenCodec(b"k"*32).cursor_codec())
    first = services.job_operations(job.id, None, 1)
    assert len(first.items) == 1 and first.next_cursor
    second = services.job_operations(job.id, first.next_cursor, 1)
    for page in (first, second):
        aggregate = page.progress.operation
        assert aggregate is not None
        assert aggregate.completed_bytes == 30
        assert aggregate.total_bytes == 300
        assert aggregate.bytes_per_second == 30.0
        assert aggregate.eta_seconds == 9.0
        assert len(aggregate.members) == 2


def test_stored_evidence_projections_keep_absence_and_corruption_distinct() -> None:
    """A present but unreadable decoration must not be reported as absent.

    The projections used to swallow a validation failure into ``None``, which
    the operation detail then surfaced as an omitted field.
    """

    assert operation_api._provenance_projection(None) is None
    assert operation_api._provenance_projection({}) is None
    assert operation_api._provenance_projection({"provenance": None}) is None
    with pytest.raises(BoundedJSONError, match="provenance is invalid"):
        operation_api._provenance_projection({"provenance": {"source": 7}})
    with pytest.raises(BoundedJSONError, match="provenance is invalid"):
        operation_api._provenance_projection({"provenance": ["not", "an", "object"]})

    assert operation_api._evidence_download_projection(None) is None
    assert operation_api._evidence_download_projection({}) is None
    assert (
        operation_api._evidence_download_projection({"evidence_download": None}) is None
    )
    with pytest.raises(BoundedJSONError, match="evidence download is invalid"):
        operation_api._evidence_download_projection(
            {"evidence_download": {"media_type": "application/json"}}
        )


def test_corrupt_stored_evidence_decoration_is_a_declared_server_fault() -> None:
    """The operation detail route must not answer a corrupt decoration with 200.

    The route declares 503, so a stored provenance document that no longer
    validates is reported there instead of escaping as an undeclared 500.
    """

    now = datetime(2026, 8, 5, tzinfo=UTC)
    value: dict[str, object] = {
        "id": "11111111-1111-4111-8111-111111111111",
        "attempt": 1,
        "kind": "node.probe",
        "node_ids": [NODE_ID],
        "state": "succeeded",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "result": {"provenance": {"source": 7}},
    }

    def unavailable(*_args: object) -> NoReturn:
        raise AssertionError("not used")

    services = OperationApiServices(
        endpoint=lambda _alias: {},
        agents=lambda: (),
        job_operations=unavailable,
        resume_job=lambda _job_id: None,
        list_operations=unavailable,
        get_operation=lambda _operation_id: value,
    )
    client, operator, *_ = _client(operations=services)

    detail = client.get(f"/api/operations/{value['id']}", headers=operator)
    assert detail.status_code == 503
    assert detail.json()["detail"] == "operation projection unavailable"
