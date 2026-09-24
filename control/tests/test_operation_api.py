from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control import operation_api
from vonk_control.agent_jobs import (
    AgentJobService,
    OperatorRetirementRefused,
    OperatorRetryExhausted,
)
from vonk_control.agent_upgrade_status import operator_agent_upgrade_reason
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.bounded_json import BoundedJSONError
from vonk_control.fleet_profile_contract import (
    FleetProfileDefinition,
    FleetProfileEffects,
    FleetProfilePlanStep,
    FleetProfilePlanSummary,
    FleetProfilePreview,
    FleetProfileScopePreview,
)
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.fleet_projection import FleetSnapshot
from vonk_control.jobs import JobService
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    AuditEvent,
    Base,
    FleetProfile,
    FleetProfileApplication,
    Job,
    RecipeRun,
    ResourceReservation,
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
from vonk_control.recovery_policy import RecoveryPolicy
from vonk_control.strict_json import serialize_json_value

from .runtime_identity_support import claim_agent

COMMIT = "a" * 64
DIGEST = "d" * 64
NODE_ID = "spk_" + "1" * 32
PARKED_NODE_ID = "spk_" + "2" * 32
PARKED_CAPABILITIES = (
    "agent.runtime.rust.v1",
    "recipe.stop",
    "agent.lifecycle.resume.exact.v1",
)
PARKED_PAYLOAD = {
    "schema_version": 1,
    "run_id": "00000000-0000-4000-8000-000000000001",
    "plan_digest": COMMIT,
}


class MutableClock:
    """A frozen clock the resume tests advance past a scheduled retry."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _profile_operation_plan(
    *,
    profile_id: str,
    profile_digest: str,
    plan_digest: str,
    node_ids: list[str],
    now: datetime,
) -> dict[str, object]:
    """Build the same complete preview document persisted by profile apply."""

    return FleetProfilePreview(
        profile_id=profile_id,
        profile_name="Studio",
        profile_digest=profile_digest,
        profile_revision=1,
        profile_definition=FleetProfileDefinition(name="Studio"),
        resolved_assignments=[],
        preparation_decisions=[],
        admission_decisions=[],
        assessments=[],
        effects=FleetProfileEffects(runs=[], installations=[], superseded=[]),
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
                index=0, kind="switch", node_ids=node_ids, label="Apply profile"
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


def _client(*, fleet_projection=None, operations=None, role="operator", jobs=None):
    codec = TokenCodec(b"k" * 32)
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs() if jobs is None else jobs,
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


def _durable_client(sessions, services, *, clock: MutableClock):
    return _client(
        operations=services,
        jobs=JobService(sessions, clock=clock),
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
        list_operations=lambda _cursor, _limit, _state, _node_id, _request_id: (
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


def test_operation_read_surfaces_a_recorded_claim_refusal_reason() -> None:
    from types import SimpleNamespace

    reason = (
        "claim refused: live-mutation-in-progress "
        "(operation_intent=4; blocking_kind=recipe.start; blocking_intent=2)"
    )
    item = {
        "id": "33333333-3333-4333-8333-333333333333",
        "parent_id": None,
        "node_ids": [NODE_ID],
        "kind": "artifact.distribution.v1",
        "state": "queued",
        "attempt": 0,
        "progress": None,
        "result": None,
        "created_at": "2026-08-15T11:59:00Z",
        "updated_at": "2026-08-15T12:00:00Z",
        "status_reason": reason,
    }

    response = operation_api.operation_detail_response(item)

    assert response.status_reason == reason
    assert response.model_dump()["status_reason"] == reason
    # A claim that was never refused keeps the response shape it always had.
    assert (
        "status_reason"
        not in operation_api.operation_detail_response(
            {**item, "status_reason": None}
        ).model_dump()
    )

    operation = SimpleNamespace(
        current_attempt=0,
        id=item["id"],
        kind=item["kind"],
        node_id=NODE_ID,
        parent_job_id=None,
        payload={},
        state="queued",
        status_reason=reason,
        updated_at=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
    )
    assert (
        operation_api._operation_item(cast(AgentOperation, operation), None)[
            "status_reason"
        ]
        == reason
    )


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
    assert serialize_json_value(
        operation_api._progress_projection({"phase": "verify"})
    ) == {
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
    tmp_path,
    profile_state,
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
                        if profile_state != "running"
                        else None
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
        "phase": "prepare",
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


def test_activity_page_survives_one_unreadable_profile_application(tmp_path) -> None:
    """A damaged record must not fail the global Activity page or its detail."""

    engine = create_engine(f"sqlite:///{tmp_path / 'profile-damaged.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    profile_id = "44444444-4444-4444-8444-444444444444"
    valid_id = "55555555-5555-4555-8555-555555555555"
    damaged_id = "66666666-6666-4666-8666-666666666666"
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
        session.add(
            FleetProfileApplication(
                id=valid_id,
                request_key="77777777-7777-4777-8777-777777777777",
                profile_id=profile_id,
                profile_digest="9" * 64,
                plan_digest="7" * 64,
                state="running",
                plan=_profile_operation_plan(
                    profile_id=profile_id,
                    profile_digest="9" * 64,
                    plan_digest="7" * 64,
                    node_ids=[NODE_ID],
                    now=now,
                ),
                current_step=0,
                current_operation_id=None,
                progress={
                    "operation_kind": "fleet-profile.apply",
                    "completed_steps": 0,
                    "total_steps": 1,
                },
                result=None,
                status_reason=None,
                actor="admin",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            FleetProfileApplication(
                id=damaged_id,
                request_key="88888888-8888-4888-8888-888888888888",
                profile_id=profile_id,
                profile_digest="9" * 64,
                plan_digest="8" * 64,
                state="running",
                # A step list that cannot be decoded into the canonical plan.
                plan={"steps": []},
                current_step=0,
                current_operation_id=None,
                progress={
                    "operation_kind": "fleet-profile.apply",
                    "completed_steps": 0,
                    "total_steps": 1,
                },
                result=None,
                status_reason=None,
                actor="admin",
                created_at=now,
                updated_at=now,
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

    page = client.get("/api/operations", headers=operator)
    detail = client.get(f"/api/operations/{damaged_id}", headers=operator)

    assert page.status_code == 200
    assert page.json()["total"] == 2
    items = {item["id"]: item for item in page.json()["operations"]}
    assert items[valid_id].get("failure") is None
    assert items[valid_id]["node_ids"] == [NODE_ID]
    assert items[damaged_id]["failure"] is not None
    assert items[damaged_id]["recovery"]["actions"] == ["inspect"]
    assert detail.status_code == 200
    assert detail.json()["id"] == damaged_id
    assert detail.json()["failure"] is not None


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
        "recovery": {"actions": ["inspect"], "uncertain": False},
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


def test_operator_resume_reports_an_exhausted_retry_budget() -> None:
    """A spent budget is a loud typed conflict, not a queued no-op."""

    def exhausted(_job_id: str) -> None:
        raise OperatorRetryExhausted("op-1", "recipe.stop", 5, 5)

    services = OperationApiServices(
        endpoint=lambda _alias: {},
        agents=lambda: (),
        job_operations=lambda _job_id, _cursor, _limit: OperationPage(
            (), None, JobProgress(completed=0, failed=0, running=0, total=0)
        ),
        resume_job=exhausted,
    )
    client, operator, *_ = _client(operations=services)
    job_id = "11111111-1111-4111-8111-111111111111"

    response = client.post(f"/api/jobs/{job_id}/resume", headers=operator)

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "operation op-1 (recipe.stop) exhausted its 5-attempt retry budget at attempt 5"
    )


def test_durable_resume_has_one_atomic_winner(tmp_path) -> None:
    clock = MutableClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    sessions, jobs, services, operation, job_id = _parked_stop_services(
        tmp_path, clock=clock
    )
    first = _claim_parked(jobs)
    assert first is not None
    jobs.wait_for_operator(first, "operator must inspect the effect")

    services.resume_job(job_id)

    resumed = _claim_parked(jobs)
    assert resumed is not None
    assert resumed.operation_id == operation.id
    assert resumed.attempt == first.attempt + 1
    with sessions() as session:
        stored_job = session.get(Job, job_id)
        stored_operation = session.get(AgentOperation, operation.id)
        assert stored_job is not None and stored_job.state == "queued"
        assert stored_operation is not None
        assert stored_operation.retry_disposition == "retry"
        assert stored_operation.retry_disposition_attempt == first.attempt


@pytest.mark.parametrize(
    "changed_authority", ["current_node", "other_target", "revocation"]
)
def test_resume_action_disappearing_after_preflight_is_refused(
    tmp_path, changed_authority
) -> None:
    """The POST rechecks owner intent after a previously valid GET."""

    clock = MutableClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    sessions, jobs, services, operation, job_id = _parked_stop_services(
        tmp_path, clock=clock
    )
    other_node_id = "spk_" + "3" * 32
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=other_node_id,
                state="active",
                protocol_version=3,
                workload_intent_ordinal=1,
            )
        )
        parent = session.get(Job, job_id)
        assert parent is not None
        parent.targets = [PARKED_NODE_ID, other_node_id]
    claim = _claim_parked(jobs)
    assert claim is not None
    jobs.wait_for_operator(claim, "operator must inspect the effect")
    client, operator, _reconciler, _audits = _durable_client(
        sessions, services, clock=clock
    )

    preflight = client.get(f"/api/jobs/{job_id}", headers=operator)
    assert preflight.status_code == 200
    assert "resume" in preflight.json()["recovery"]["actions"]
    activity = client.get("/api/operations", headers=operator)
    activity_item = next(
        row for row in activity.json()["operations"] if row["id"] == operation.id
    )
    assert "resume" in activity_item["recovery"]["actions"]
    with sessions.begin() as session:
        node = session.get(
            AgentNode,
            other_node_id if changed_authority == "other_target" else PARKED_NODE_ID,
        )
        assert node is not None
        if changed_authority == "revocation":
            node.revoked_at = clock.now
            node.state = "revoked"
        else:
            node.workload_intent_ordinal = 2

    response = client.post(
        f"/api/jobs/{job_id}/resume",
        headers=operator,
        json={"disposition": "resume"},
    )

    assert response.status_code == 409
    after_intent_change = client.get("/api/operations", headers=operator)
    after_item = next(
        row
        for row in after_intent_change.json()["operations"]
        if row["id"] == operation.id
    )
    assert "resume" not in after_item["recovery"]["actions"]
    with sessions() as session:
        parent = session.get(Job, job_id)
        stored = session.get(AgentOperation, operation.id)
        assert parent is not None and parent.state == "waiting-for-operator"
        assert stored is not None and stored.state == "waiting-for-operator"
        assert stored.retry_disposition is None
        assert stored.retry_disposition_attempt is None


def test_durable_resume_refuses_exhausted_budget_without_half_transition(
    tmp_path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    sessions, jobs, services, operation, job_id = _parked_stop_services(
        tmp_path, clock=clock
    )
    limit = _exhaust_operator_retry_budget(sessions, jobs, services, operation)
    client, operator, *_ = _durable_client(sessions, services, clock=clock)

    preflight = client.get(f"/api/jobs/{job_id}", headers=operator)
    assert preflight.status_code == 200
    assert "resume" not in preflight.json()["recovery"]["actions"]
    response = client.post(
        f"/api/jobs/{job_id}/resume",
        headers=operator,
        json={"disposition": "resume"},
    )

    assert response.status_code == 409
    assert f"{limit}-attempt retry budget" in response.json()["detail"]
    with sessions() as session:
        parent = session.get(Job, job_id)
        stored = session.get(AgentOperation, operation.id)
        assert parent is not None and parent.state == "waiting-for-operator"
        assert stored is not None and stored.state == "waiting-for-operator"
        assert stored.retry_disposition == "retry"
        assert stored.retry_disposition_attempt == limit - 1
        assert stored.current_attempt == limit


def test_durable_retire_retains_uncertain_run_capacity(tmp_path) -> None:
    """Ending an exhausted order does not prove its remote effect stopped."""

    clock = MutableClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    sessions, jobs, services, operation, job_id = _parked_stop_services(
        tmp_path, clock=clock
    )
    owner_id = str(uuid.uuid4())
    with sessions.begin() as session:
        parent = session.get(Job, job_id)
        assert parent is not None
        parent.payload = {
            **parent.payload,
            "owner_kind": "run",
            "owner_id": owner_id,
        }
        session.add(
            RecipeRun(
                id=owner_id,
                installation_id=str(uuid.uuid4()),
                mapping_id=str(uuid.uuid4()),
                mapping_generation=1,
                alias="retire-me",
                plan_digest=COMMIT,
                plan={},
                state="starting",
                route_state="withdrawn",
                actor="operator",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            ResourceReservation(
                node_id=PARKED_NODE_ID,
                kind="unified-memory",
                resource_key=COMMIT,
                amount_bytes=1024,
                owner_kind="run",
                owner_id=owner_id,
                state="active",
                plan_digest=COMMIT,
                created_at=clock.now,
            )
        )
    limit = _exhaust_operator_retry_budget(sessions, jobs, services, operation)

    _retire_parked(services, job_id)

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        parent = session.get(Job, job_id)
        run = session.get(RecipeRun, owner_id)
        reservation = session.scalar(
            select(ResourceReservation).where(ResourceReservation.owner_id == owner_id)
        )
        assert stored is not None and parent is not None
        assert run is not None and reservation is not None
        assert stored.state == "failed"
        assert stored.retry_disposition is None and stored.retry_due_at is None
        assert f"{limit}-attempt retry budget was spent" in (stored.status_reason or "")
        assert "operator retired" in (stored.status_reason or "")
        assert parent.state == "failed"
        assert parent.status_reason == stored.status_reason
        assert run.state == "lost"
        assert run.route_state == "withdrawn"
        assert reservation.state == "active"
        assert reservation.released_at is None


def test_durable_retire_refuses_live_current_attempt_without_transition(
    tmp_path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    sessions, jobs, services, operation, job_id = _parked_stop_services(
        tmp_path, clock=clock
    )
    limit = _exhaust_operator_retry_budget(sessions, jobs, services, operation)
    with sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == limit,
            )
        )
        assert attempt is not None
        attempt.state = "running"
        attempt.lease_deadline = clock.now + timedelta(seconds=30)
    client, operator, _reconciler, audits = _durable_client(
        sessions, services, clock=clock
    )

    response = client.post(
        f"/api/jobs/{job_id}/resume",
        headers=operator,
        json={"disposition": "retire"},
    )

    assert response.status_code == 409
    assert "a live attempt still holds its lease" in response.json()["detail"]
    with sessions() as session:
        parent = session.get(Job, job_id)
        stored = session.get(AgentOperation, operation.id)
        current_attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == limit,
            )
        )
        assert parent is not None and parent.state == "waiting-for-operator"
        assert stored is not None and stored.state == "waiting-for-operator"
        assert current_attempt is not None and current_attempt.state == "running"
    assert audits.list() == []


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


def test_activity_sql_pages_equal_timestamps_and_binds_request_filter(tmp_path) -> None:
    """The real provider uses ID as a stable tie-breaker and binds filters."""

    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'activity-pages.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    request_id = "33333333-3333-4333-8333-333333333333"
    other_request_id = "44444444-4444-4444-8444-444444444444"
    second_node_id = "spk_" + "2" * 32
    job = Job(
        request_id=request_id,
        kind="runtime.preflight.v1",
        state="succeeded",
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
        session.add(
            Job(
                id="22222222-2222-4222-8222-222222222222",
                request_id="55555555-5555-4555-8555-555555555555",
                kind="agent-upgrade",
                state="succeeded",
                actor="operator",
                authority_revision=COMMIT,
                targets=[NODE_ID],
                payload_digest="f" * 64,
                payload={},
                current_attempt=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            Job(
                id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                request_id="99999999-9999-4999-8999-999999999999",
                kind="agent-upgrade",
                state="succeeded",
                actor="operator",
                authority_revision=COMMIT,
                targets=[second_node_id],
                payload_digest="c" * 64,
                payload={},
                current_attempt=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            [
                AuditEvent(
                    id="33333333-3333-4333-8333-333333333333",
                    request_id=request_id,
                    actor="operator",
                    action="job.resume",
                    authority_revision=COMMIT,
                    targets=[NODE_ID],
                    occurred_at=now,
                ),
                AuditEvent(
                    id="44444444-4444-4444-8444-444444444444",
                    request_id=other_request_id,
                    actor="operator",
                    action="fleet.read",
                    authority_revision=None,
                    targets=[NODE_ID],
                    occurred_at=now,
                ),
            ]
        )
        session.flush()
        for suffix in ("001", "002", "003"):
            session.add(
                AgentOperation(
                    id=f"11111111-1111-4111-8111-000000000{suffix}",
                    parent_job_id=job.id,
                    node_id=NODE_ID,
                    kind="runtime.preflight.v1",
                    payload_digest="a" * 64,
                    payload={},
                    authority_revision=COMMIT,
                    state="succeeded",
                    current_attempt=1,
                    created_at=now,
                    updated_at=now,
                )
            )
    services = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=TokenCodec(b"p" * 32).cursor_codec(),
    )
    client, operator, *_ = _durable_client(sessions, services, clock=MutableClock(now))

    first = client.get(
        "/api/operations",
        headers=operator,
        params={"limit": 1, "request_id": request_id},
    )
    second = client.get(
        "/api/operations",
        headers=operator,
        params={
            "limit": 1,
            "request_id": request_id,
            "cursor": first.json()["next_cursor"],
        },
    )
    unbound = client.get(
        "/api/operations",
        headers=operator,
        params={"limit": 1, "cursor": first.json()["next_cursor"]},
    )
    filtered = client.get(
        "/api/operations",
        headers=operator,
        params={"request_id": other_request_id},
    )
    target_filtered = client.get(
        "/api/operations",
        headers=operator,
        params={"node_id": NODE_ID},
    )
    all_ids: list[str] = []
    cursor = None
    while True:
        params: dict[str, str | int] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = client.get("/api/operations", headers=operator, params=params)
        assert page.status_code == 200
        assert page.json()["total"] == 6
        all_ids.extend(item["id"] for item in page.json()["operations"])
        cursor = page.json().get("next_cursor")
        if cursor is None:
            break

    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == 3
    assert first.json()["operations"][0]["id"].endswith("003")
    assert second.json()["operations"][0]["id"].endswith("002")
    assert first.json()["operations"][0]["owner"] == {
        "kind": "job",
        "id": job.id,
        "request_id": request_id,
    }
    assert unbound.status_code == 422
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    audit = filtered.json()["operations"][0]
    assert audit["owner"] == {
        "kind": "audit-event",
        "id": "44444444-4444-4444-8444-444444444444",
        "request_id": other_request_id,
    }
    assert target_filtered.status_code == 200
    assert target_filtered.json()["total"] == 5
    assert all(
        NODE_ID in row["node_ids"] for row in target_filtered.json()["operations"]
    )
    assert all_ids == sorted(all_ids, reverse=True)
    assert len(all_ids) == len(set(all_ids)) == 6
    assert any(item_id.startswith("job:") for item_id in all_ids)
    assert any(item_id.startswith("audit:") for item_id in all_ids)


def test_activity_keeps_good_audit_refs_visible_beside_malformed_history(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / 'activity-malformed.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    good_request = "66666666-6666-4666-8666-666666666666"
    malformed_request = "77777777-7777-4777-8777-777777777777"
    with sessions.begin() as session:
        session.add_all(
            [
                AuditEvent(
                    id="88888888-8888-4888-8888-888888888888",
                    request_id=good_request,
                    actor="operator",
                    action="fleet.read",
                    authority_revision=None,
                    targets=[NODE_ID],
                    occurred_at=now,
                ),
                AuditEvent(
                    id="99999999-9999-4999-8999-999999999999",
                    request_id=malformed_request,
                    actor="operator",
                    action="fleet.read",
                    authority_revision=None,
                    targets=cast(list[str], {"invalid": "targets"}),
                    occurred_at=now,
                ),
            ]
        )
    services = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=TokenCodec(b"m" * 32).cursor_codec(),
    )
    client, operator, *_ = _durable_client(sessions, services, clock=MutableClock(now))

    response = client.get("/api/operations", headers=operator)
    denied = client.get("/api/operations")

    assert response.status_code == 200
    assert response.json()["total"] == 2
    items = {row["owner"]["request_id"]: row for row in response.json()["operations"]}
    assert items[good_request]["kind"] == "audit.fleet.read"
    assert items[malformed_request]["state"] == "unavailable"
    assert items[malformed_request]["failure"]["error_code"] == (
        "operation_history_unreadable"
    )
    assert denied.status_code == 401


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
        queued_target["retry_not_before"] == (now + timedelta(seconds=240)).isoformat()
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
    assert "Resume queues the retry behind a new safety delay" in specific_next_action


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
    job = Job(
        request_id="33333333-3333-4333-8333-333333333333",
        kind="reconcile",
        state="running",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_ID, "spk_" + "2" * 32],
        payload_digest="e" * 64,
        payload={},
        current_attempt=1,
        created_at=now,
        updated_at=now,
    )
    with sessions.begin() as session:
        session.add(job)
        session.flush()
        for index, target in enumerate(job.targets, 1):
            session.add(AgentNode(node_id=target, state="active", capabilities=[]))
            operation = AgentOperation(
                parent_job_id=job.id,
                node_id=target,
                kind="node.probe",
                payload_digest=f"{index:064x}",
                payload={},
                authority_revision=COMMIT,
                state="running",
                current_attempt=1,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            session.flush()
            session.add(
                AgentOperationAttempt(
                    operation_id=operation.id,
                    attempt=1,
                    fence=f"00000000-0000-4000-8000-{index:012d}",
                    lease_deadline=now + timedelta(minutes=1),
                    agent_certificate_serial=f"serial-{index}",
                    state="running",
                    progress={
                        "phase": "copying",
                        "completed_bytes": index * 10,
                        "total_bytes": index * 100,
                        "total_bytes_known": True,
                        "bytes_per_second": float(index * 10),
                        "eta_seconds": 9.0,
                        "observed_at": now.isoformat(),
                        "last_progress_at": now.isoformat(),
                    },
                )
            )
    services = operation_api.durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )
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

    identifier = "11111111-1111-4111-8111-111111111111"

    assert operation_api._provenance_projection(None, identifier) is None
    assert operation_api._provenance_projection({}, identifier) is None
    assert (
        operation_api._provenance_projection({"provenance": None}, identifier) is None
    )
    with pytest.raises(
        BoundedJSONError, match="provenance for operation .* is invalid"
    ):
        operation_api._provenance_projection({"provenance": {"source": 7}}, identifier)
    with pytest.raises(
        BoundedJSONError, match="provenance for operation .* is invalid"
    ):
        operation_api._provenance_projection(
            {"provenance": ["not", "an", "object"]}, identifier
        )

    assert operation_api._evidence_download_projection(None, identifier) is None
    assert operation_api._evidence_download_projection({}, identifier) is None
    assert (
        operation_api._evidence_download_projection(
            {"evidence_download": None}, identifier
        )
        is None
    )
    with pytest.raises(
        BoundedJSONError, match="evidence download for operation .* is invalid"
    ):
        operation_api._evidence_download_projection(
            {"evidence_download": {"media_type": "application/json"}}, identifier
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
        # The list route decorates each item, so it reaches the same corrupt
        # document through the projection rather than through a failing call.
        list_operations=lambda *_args: OperationListPage((value,), None, 1),
        get_operation=lambda _operation_id: value,
    )
    client, operator, *_ = _client(operations=services)

    detail = client.get(f"/api/operations/{value['id']}", headers=operator)
    assert detail.status_code == 503
    assert detail.json()["detail"] == (
        f"stored provenance for operation {value['id']} is invalid"
    )

    listed = client.get("/api/operations", headers=operator)
    assert listed.status_code == 200
    assert listed.json()["operations"][0]["id"] == value["id"]
    assert listed.json()["operations"][0]["kind"] == "unreadable"
    assert listed.json()["operations"][0]["failure"]["error_code"] == (
        "operation_history_unreadable"
    )


def test_agent_upgrade_diagnostics_distinguish_absent_from_corrupt() -> None:
    """A package document that is present but unreadable must not read as absent."""

    engine = create_engine("sqlite://")
    job_table = Job.__table__
    assert isinstance(job_table, Table)
    Base.metadata.create_all(engine, tables=[job_table])
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 5, tzinfo=UTC)

    def stored(payload: dict[str, object]) -> str:
        job_id = str(uuid.uuid4())
        with sessions.begin() as session:
            session.add(
                Job(
                    id=job_id,
                    request_id=str(uuid.uuid4()),
                    kind="agent-upgrade",
                    state="running",
                    actor="admin",
                    authority_revision=COMMIT,
                    targets=[NODE_ID],
                    payload_digest="a" * 64,
                    payload=payload,
                    created_at=now,
                    updated_at=now,
                )
            )
        return job_id

    def diagnostics(job_id: str) -> object:
        with sessions() as session:
            return operation_api._agent_upgrade_diagnostics(session, job_id)

    # An upgrade that carries no package document has no diagnostics to project.
    assert diagnostics(stored({})) is None
    assert diagnostics(stored({"package": None})) is None
    # A present but non-object package is corruption, and the message names the
    # job so the corrupt row can be found.
    corrupt = stored({"package": "not-a-document"})
    with pytest.raises(BoundedJSONError, match=f"{corrupt} package payload is invalid"):
        diagnostics(corrupt)


def _parked_stop_services(tmp_path, *, clock: MutableClock):
    """Build the durable rows one real parked ``recipe.stop`` needs.

    Production resumes and claims in separate transactions, so the test keeps
    the agent queue that parks and claims apart from the operator projection
    that resumes.  The node advertises the exact-resume capability a lifecycle
    operation must hold before a retry is offered to it.
    """

    engine = create_engine(
        f"sqlite:///{tmp_path / 'parked-resume.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    payload = {"workload_intent_ordinal": 1}
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=PARKED_NODE_ID,
                state="active",
                protocol_version=3,
                workload_intent_ordinal=1,
                capabilities=list(PARKED_CAPABILITIES),
                architecture="linux-arm64",
                semantic_version="1.0.0",
                build_digest="sha256:" + "f" * 64,
                binary_digest="f" * 64,
                self_test_passed=True,
            )
        )
        session.add(
            AgentCertificate(
                serial="serial-parked",
                node_id=PARKED_NODE_ID,
                not_before=clock.now - timedelta(seconds=1),
                not_after=clock.now + timedelta(hours=1),
                fingerprint="fingerprint-parked",
            )
        )
        job = Job(
            request_id="44444444-4444-4444-8444-444444444444",
            kind="agent.operations",
            state="queued",
            actor="operator",
            authority_revision=COMMIT,
            targets=[PARKED_NODE_ID],
            payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
            payload=payload,
            current_attempt=0,
            created_at=clock.now,
            updated_at=clock.now,
        )
        session.add(job)
        session.flush()
        job_id = job.id
    jobs = AgentJobService(sessions, clock=clock)
    operation = jobs.enqueue(
        job_id, PARKED_NODE_ID, "recipe.stop", COMMIT, PARKED_PAYLOAD
    )
    services = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=clock,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )
    return sessions, jobs, services, operation, job_id


def _claim_parked(jobs):
    return claim_agent(
        jobs,
        PARKED_NODE_ID,
        "serial-parked",
        30,
        protocol_version=3,
        capabilities=PARKED_CAPABILITIES,
    )


def _exhaust_operator_retry_budget(sessions, jobs, services, operation):
    """Spend explicit retries on an effect without safe automatic reconciliation.

    Exact restart-safe interruption now retries for as long as intent remains
    current. An unclassified parked effect still needs one operator decision per
    claim and retains the bounded operator budget tested by resume/retirement.
    """

    limit = RecoveryPolicy().max_failures
    for expected_attempt in range(1, limit + 1):
        claim = _claim_parked(jobs)
        assert claim is not None and claim.attempt == expected_attempt
        jobs.wait_for_operator(claim, "effect requires operator inspection")
        with sessions() as session:
            stored = session.get(AgentOperation, operation.id)
            assert stored is not None
            assert not (
                stored.retry_disposition == "retry"
                and stored.retry_disposition_attempt == stored.current_attempt
            )
        if expected_attempt < limit:
            services.resume_job(claim.job_id)
    return limit


def test_durable_resume_authorises_the_parked_operation_for_the_next_claim(
    tmp_path,
) -> None:
    """A 200 from resume must release the operation, not only the parent job.

    The claim predicate requires the parked operation's own retry
    authorisation, so a resume that wrote only ``Job.state`` returned success
    while the operation stayed unclaimable forever.
    """

    clock = MutableClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    sessions, jobs, services, operation, job_id = _parked_stop_services(
        tmp_path, clock=clock
    )
    first = _claim_parked(jobs)
    assert first is not None
    jobs.wait_for_operator(first, "operator must inspect the effect")

    services.resume_job(job_id)

    resumed = _claim_parked(jobs)
    assert resumed is not None
    assert resumed.operation_id == operation.id
    assert resumed.attempt == first.attempt + 1
    with sessions() as session:
        parent = session.get(Job, job_id)
        assert parent is not None and parent.state == "queued"


def test_durable_resume_refuses_a_job_that_is_not_parked(tmp_path) -> None:
    clock = MutableClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    engine = create_engine(f"sqlite:///{tmp_path / 'not-parked.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    job = Job(
        request_id="55555555-5555-4555-8555-555555555555",
        kind="reconcile",
        state="queued",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_ID],
        payload_digest="e" * 64,
        payload={},
        current_attempt=1,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(job)
    services = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=clock,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
    )

    with pytest.raises(ValueError, match="job is not waiting for operator"):
        services.resume_job(job.id)

    with sessions() as session:
        stored = session.get(Job, job.id)
        assert stored is not None and stored.state == "queued"


def test_durable_resume_refuses_an_exhausted_operator_retry_budget(tmp_path) -> None:
    """A spent budget must refuse loudly instead of queueing dead work.

    Each attempt needs an explicit resume because its effect cannot be safely
    retried automatically. Safe ongoing retries are covered separately; they do
    not consume this operator-only budget.
    """

    clock = MutableClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    sessions, jobs, services, operation, job_id = _parked_stop_services(
        tmp_path, clock=clock
    )
    limit = _exhaust_operator_retry_budget(sessions, jobs, services, operation)
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        parent = session.get(Job, job_id)
        assert stored is not None and parent is not None
        assert not (
            stored.retry_disposition == "retry"
            and stored.retry_disposition_attempt == stored.current_attempt
        )
        assert stored.current_attempt == limit
        assert parent.state == "waiting-for-operator"

    with pytest.raises(OperatorRetryExhausted) as refusal:
        services.resume_job(job_id)

    assert refusal.value.operation_id == operation.id
    assert refusal.value.limit == limit
    with sessions() as session:
        parent = session.get(Job, job_id)
        assert parent is not None and parent.state == "waiting-for-operator"


def test_operator_retire_is_a_distinct_audited_disposition() -> None:
    """Retirement is requested explicitly; the default request still resumes."""

    resumed: list[str] = []
    retired: list[str] = []
    services = OperationApiServices(
        endpoint=lambda _alias: {},
        agents=lambda: (),
        job_operations=lambda _job_id, _cursor, _limit: OperationPage(
            (), None, JobProgress(completed=0, failed=0, running=0, total=0)
        ),
        resume_job=resumed.append,
        retire_job=retired.append,
    )
    client, operator, _reconciler, audits = _client(operations=services)
    job_id = "11111111-1111-4111-8111-111111111111"

    resumed_request = "33333333-3333-4333-8333-333333333333"
    default_response = client.post(
        f"/api/jobs/{job_id}/resume",
        headers={**operator, "X-Request-ID": resumed_request},
    )
    retired_request = "33333333-3333-4333-8333-333333333334"
    retired_response = client.post(
        f"/api/jobs/{job_id}/resume",
        headers={**operator, "X-Request-ID": retired_request},
        json={"disposition": "retire"},
    )

    assert default_response.status_code == 202
    assert default_response.json() == {"id": job_id, "state": "queued"}
    assert resumed == [job_id]
    assert retired == [job_id]
    assert retired_response.status_code == 202
    assert retired_response.json() == {"id": job_id, "state": "failed"}
    assert audits.for_request(resumed_request).action == "job.resume"
    assert audits.for_request(retired_request).action == "job.retire"


def test_operator_retire_reports_a_live_operation_refusal() -> None:
    """A refusal is a typed 409, never a silent terminal transition."""

    def refused(_job_id: str) -> None:
        raise OperatorRetirementRefused("op-1", "its bounded retry budget is not spent")

    services = OperationApiServices(
        endpoint=lambda _alias: {},
        agents=lambda: (),
        job_operations=lambda _job_id, _cursor, _limit: OperationPage(
            (), None, JobProgress(completed=0, failed=0, running=0, total=0)
        ),
        resume_job=lambda _job_id: None,
        retire_job=refused,
    )
    client, operator, *_ = _client(operations=services)

    response = client.post(
        "/api/jobs/11111111-1111-4111-8111-111111111111/resume",
        headers=operator,
        json={"disposition": "retire"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "operation op-1 cannot be retired: its bounded retry budget is not spent"
    )


def _retire_parked(services, job_id: str) -> None:
    """Call the optional retirement projection, failing if it is unavailable."""

    retire = services.retire_job
    assert retire is not None
    retire(job_id)


def test_durable_retire_fails_the_order_but_retains_uncertain_capacity(
    tmp_path,
) -> None:
    """The retry budget says nothing about an abandoned container's liveness."""

    clock = MutableClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    sessions, jobs, services, operation, job_id = _parked_stop_services(
        tmp_path, clock=clock
    )
    owner_id = str(uuid.uuid4())
    with sessions.begin() as session:
        parent = session.get(Job, job_id)
        assert parent is not None
        parent.payload = {
            **parent.payload,
            "owner_kind": "run",
            "owner_id": owner_id,
        }
        session.add(
            RecipeRun(
                id=owner_id,
                installation_id=str(uuid.uuid4()),
                mapping_id=str(uuid.uuid4()),
                mapping_generation=1,
                alias="retire-me",
                plan_digest=COMMIT,
                plan={},
                state="starting",
                route_state="withdrawn",
                actor="operator",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            ResourceReservation(
                node_id=PARKED_NODE_ID,
                kind="unified-memory",
                resource_key=COMMIT,
                amount_bytes=1024,
                owner_kind="run",
                owner_id=owner_id,
                state="active",
                plan_digest=COMMIT,
                created_at=clock.now,
            )
        )
    limit = _exhaust_operator_retry_budget(sessions, jobs, services, operation)

    _retire_parked(services, job_id)

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        parent = session.get(Job, job_id)
        run = session.get(RecipeRun, owner_id)
        reservation = session.scalar(
            select(ResourceReservation).where(ResourceReservation.owner_id == owner_id)
        )
        assert stored is not None and parent is not None
        assert run is not None and reservation is not None
        assert stored.state == "failed"
        assert stored.retry_disposition is None and stored.retry_due_at is None
        assert f"{limit}-attempt retry budget was spent" in (stored.status_reason or "")
        assert "operator retired" in (stored.status_reason or "")
        assert parent.state == "failed"
        assert parent.status_reason == stored.status_reason
        assert run.state == "lost"
        assert run.route_state == "withdrawn"
        assert reservation.state == "active"
        assert reservation.released_at is None


def test_durable_retire_refuses_a_parked_operation_with_budget_remaining(
    tmp_path,
) -> None:
    """An operation that can still progress must never be silently discarded."""

    clock = MutableClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    sessions, jobs, services, operation, job_id = _parked_stop_services(
        tmp_path, clock=clock
    )
    first = _claim_parked(jobs)
    assert first is not None
    jobs.wait_for_operator(first, "operator must inspect the effect")

    with pytest.raises(OperatorRetirementRefused) as refusal:
        _retire_parked(services, job_id)

    assert refusal.value.operation_id == operation.id
    assert "retry budget is not spent" in refusal.value.reason
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        parent = session.get(Job, job_id)
        assert stored is not None and parent is not None
        assert stored.state == "waiting-for-operator"
        assert parent.state == "waiting-for-operator"


def test_durable_retire_refuses_a_parked_operation_whose_lease_is_live(
    tmp_path,
) -> None:
    """A held lease is live work, even when the parent row looks parked."""

    clock = MutableClock(datetime(2026, 8, 5, 12, 0, tzinfo=UTC))
    sessions, _jobs, services, operation, job_id = _parked_stop_services(
        tmp_path, clock=clock
    )
    _claim_parked(_jobs)
    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
        assert attempt is not None
        # The parent was re-parked at the spent budget, but the attempt still
        # holds an unexpired lease, so the effect is live and must not be
        # abandoned.
        stored.state = "waiting-for-operator"
        stored.current_attempt = RecoveryPolicy().max_failures
        stored.retry_disposition = None
        stored.retry_disposition_attempt = None
        stored.retry_due_at = None
        attempt.attempt = stored.current_attempt
        attempt.state = "running"
        parent = session.get(Job, job_id)
        assert parent is not None
        parent.state = "waiting-for-operator"

    with pytest.raises(OperatorRetirementRefused) as refusal:
        _retire_parked(services, job_id)

    assert "holds its lease" in refusal.value.reason
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        assert stored.state == "waiting-for-operator"
