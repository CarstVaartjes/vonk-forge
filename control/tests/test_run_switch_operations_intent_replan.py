"""Run/Switch operations: intent replan."""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, update

from vonk_agent_protocol.agent_words import ProfileChildPhase, ProfileReasonSeverity
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, WaitReason
from vonk_agent_protocol.reason_codes.runs import RunSwitchCode
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.categorized_errors import BookkeepingUnknown
from vonk_control.distribution_executor.preparation import (
    CompositeDistributionPhaseExecutor,
)
from vonk_control.fleet_profile_contract.definitions import (
    FleetProfileAssignmentInput,
    FleetProfileInput,
)
from vonk_control.fleet_profiles.read_observation import profile_read
from vonk_control.fleet_profiles.run_switch_adapter import (
    RunSwitchFleetProfileAdapter,
)
from vonk_control.fleet_profiles.service import FleetProfileService
from vonk_control.model_cache.service import ModelCacheService
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.mapping import ClusterMapping, ClusterMappingNode
from vonk_control.models.telemetry import NodeInventorySnapshot
from vonk_control.run_switch_contract.evidence import RunSwitchPhase
from vonk_control.run_switch_contract.operations import (
    RunSwitchOperationResult,
)
from vonk_control.run_switch_contract.phase_results import (
    RunSwitchDistributionChildResult,
    RunSwitchPhaseResult,
)
from vonk_control.run_switch_contract.requests import (
    RunSwitchApplyRequest,
)
from vonk_control.run_switch_operations.result_helpers import _phase_result
from vonk_control.run_switch_operations.service import RunSwitchOperationService

from .non_blocking import assert_ended_without_blocking
from .recipe_operations_support import NOW, setup_services
from .run_switch_operations_support import (
    CompleteArtifactInspector,
    ModelCacheManifestProvider,
    RecordingArtifactExecutor,
    _child_operation_id,
    _replace_child,
    _request,
    _result,
    _service,
)


@pytest.mark.parametrize(
    ("kind", "subphase", "receipt"),
    [
        (
            "stop",
            None,
            {"phase": "start", "run_id": "11111111-1111-4111-8111-111111111111"},
        ),
        (
            "prepare",
            "runtime-image",
            {"phase": "prepare", "subphase": "runtime-plan", "prepared": True},
        ),
    ],
)
def test_valid_receipt_for_another_phase_cannot_enter_current_progress(
    kind, subphase, receipt
) -> None:
    TypeAdapter(RunSwitchPhaseResult).validate_python(receipt, strict=True)
    expected = RunSwitchPhase(
        index=0, kind=kind, subphase=subphase, state="planned", detail="Current phase"
    )
    with pytest.raises(Exception) as _ending:
        _phase_result(receipt, phase=expected)


def test_persisted_phase_receipts_reject_empty_and_cross_phase_shapes() -> None:
    adapter = TypeAdapter(RunSwitchPhaseResult)
    with pytest.raises(ValidationError):
        adapter.validate_python({}, strict=True)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "phase": "prepare",
                "subphase": "runtime-image",
                "runtime_image": {"image_digest": "sha256:" + "1" * 64},
                "image_digest": "sha256:" + "1" * 64,
                "oci_layout_sha256": "2" * 64,
                "image_bytes": 1,
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "phase": "transfer",
                "subphase": "model-download",
                "schema_version": 2,
                "artifact_set_sha256": "3" * 64,
                "downloaded_bytes": 0,
                "total_bytes": 0,
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "phase": "transfer",
                "subphase": "model-download",
                "schema_version": 2,
                "artifact_set_sha256": "3" * 64,
                "coverage": "complete",
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "progress": {
                    "phase": "model-download",
                    "completed_bytes": 0,
                    "total_bytes": 0,
                    "total_bytes_known": True,
                },
                "evidence": {
                    "schema_version": 2,
                    "artifact_set_sha256": "4" * 64,
                    "coverage": "complete",
                },
            },
            strict=True,
        )


def test_same_clock_later_intent_fences_older_queued_work(tmp_path: Path) -> None:
    """Database admission order, not timestamp spelling, owns the node."""

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    request = _request(sessions, nodes[0])
    first = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    second = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    with sessions() as session:
        old = session.get(Job, first.operation_id)
        new = session.get(Job, second.operation_id)
        assert old is not None
        assert new is not None
        assert old.created_at == new.created_at
        assert old.payload["workload_intent_ordinal"] == 1
        assert new.payload["workload_intent_ordinal"] == 2
    assert service._advance(first.operation_id) is True
    assert service.get(first.operation_id).state == "cancelled"
    assert service.get(second.operation_id).state == "queued"


def test_a_slow_agent_clock_does_not_make_inventory_stale_for_a_load(
    tmp_path: Path,
) -> None:
    """Catches ageing inventory by the agent stamp: a report collected 10 minutes
    before it was received (a slow Spark clock) refused every load."""
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    with sessions.begin() as session:
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.observed_at = NOW - timedelta(minutes=10)
        snapshot.received_at = NOW
    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
        artifacts=CompleteArtifactInspector(),
    )

    plan = service.preview(_request(sessions, nodes[0]), actor="admin")

    assert plan.allowed, plan.blockers
    assert RunSwitchCode.INVENTORY_STALE not in {item.code for item in plan.blockers}


def test_stale_inventory_intent_waits_and_replans_when_inventory_returns(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    with sessions.begin() as session:
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.observed_at = NOW - timedelta(days=1)
        snapshot.received_at = NOW - timedelta(days=1)
    now = [NOW]
    executor = RecordingArtifactExecutor()
    service = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(),
    )
    service._clock = lambda: now[0]
    request = _request(sessions, nodes[0])
    blocked = service.preview(request, actor="admin")
    assert blocked.allowed is False
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    assert operation.state == LifecycleState.OBSERVING
    old_installation_id = str(uuid.uuid4())
    with sessions.begin() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        progress = dict(job.result or {})
        assert job.next_action_at is not None
        first_due = job.next_action_at.replace(tzinfo=UTC)
        # Work attributed to the old plan must never be adopted by the new one.
        progress.update(
            {
                "phase_index": 1,
                "item_index": 1,
                "phase": "final_verify",
                "completed_phases": ["prepare"],
                "child_operation_id": str(uuid.uuid4()),
                "phase_results": [
                    {
                        "phase": "prepare",
                        "subphase": "runtime-install",
                        "installation_id": old_installation_id,
                    }
                ],
            }
        )
        job.result = progress
    # While blockers remain, re-plans back off exponentially.
    now[0] = first_due + timedelta(seconds=1)
    assert service._advance(operation.operation_id) is True
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        assert isinstance(job.result, dict)
        assert job.next_action_at is not None
        second_due = job.next_action_at.replace(tzinfo=UTC)
        assert job.result["retry_attempt"] == 3
    assert second_due - now[0] > first_due - NOW
    # Nothing is re-planned before the next due time.
    assert service._advance(operation.operation_id) is False
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        assert isinstance(job.result, dict)
        assert job.result["retry_attempt"] == 3
    with sessions.begin() as session:
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.observed_at = NOW
        snapshot.received_at = NOW
    now[0] = second_due + timedelta(seconds=1)
    assert service._advance(operation.operation_id) is True
    refreshed = service.get(operation.operation_id)
    assert refreshed.state == "queued"
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        assert isinstance(job.result, dict)
        assert job.result.get("phase_index") == 0
        assert job.result.get("item_index") == 0
        assert job.result.get("completed_phases") == []
        assert job.result.get("phase_results") == []
        assert job.result.get("child_operation_id") is None
        assert job.result.get("retry_attempt") is None
        # A new child identity generation: no Start or installation adopted
        # under the old plan's request keys can be reused.
        assert job.result.get("phase_retry_generation") == 1
        refreshed_plan = job.payload.get("plan")
        assert isinstance(refreshed_plan, dict)
        authority_revision = refreshed_plan.get(
            "recipe_content_sha256"
        ) or job.payload.get("plan_digest")
        assert job.authority_revision == authority_revision


@pytest.mark.parametrize("expiry_path", ["worker", "phase-failure"])
def test_run_switch_missing_target_has_retryable_dependency_recovery_bound(
    tmp_path: Path,
    expiry_path: str,
) -> None:
    """Catch unclassified exhaustion in worker observation and phase retries."""
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
        artifacts=CompleteArtifactInspector(),
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    with sessions.begin() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        job.targets = ["spk_ffffffffffffffffffffffffffffffff"]

    def end(_operation):
        assert service._advance(operation.operation_id) is True
        waiting = service.get(operation.operation_id)
        assert waiting.state == LifecycleState.RUNNING
        assert waiting.result is not None
        assert waiting.result.final_verify_started_at is None
        assert waiting.result.observation_deadline_at is not None
        deadline = waiting.result.observation_deadline_at
        service._clock = lambda: deadline + timedelta(seconds=1)
        if expiry_path == "phase-failure":
            service._fail(
                operation.operation_id,
                "target remains unavailable",
                checkpoint=(0, 0, None),
            )
        else:
            assert service._advance(operation.operation_id) is True
        return service.get(operation.operation_id)

    def typed_reason(receipt):
        assert (
            _result(receipt).failure_code == RunSwitchCode.DEPENDENCY_WINDOW_EXHAUSTED
        )
        assert _result(receipt).retryable is True
        assert _result(receipt).observation_deadline_at is not None

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        operation,
        end=end,
        fresh=lambda _world: service.apply(
            RunSwitchApplyRequest(
                **request.model_dump(), request_key=str(uuid.uuid4())
            ),
            actor="admin",
        ),
        assert_reason=typed_reason,
        request_key=lambda receipt: receipt.request_key,
    )


@pytest.mark.parametrize("repair", [False, True])
def test_inactive_target_waits_then_resumes_when_the_spark_returns(
    tmp_path: Path,
    repair: bool,
) -> None:
    """A Spark that goes inactive parks the switch; its return resumes it."""

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    now = [NOW]
    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
        artifacts=CompleteArtifactInspector(),
    )
    service._clock = lambda: now[0]
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        active_state = node.state
        node.state = NodeIdentityState.RETIRED

    assert service._advance(operation.operation_id) is True
    waiting = service.get(operation.operation_id)
    assert waiting.state == LifecycleState.OBSERVING
    assert waiting.result is not None
    assert waiting.next_attempt_at
    due = waiting.next_attempt_at
    # Nothing happens before the backoff is due.
    assert service._advance(operation.operation_id) is False
    if not repair:
        # Restart the observer with the target still away, hours past the
        # old fixed dependency budget: a Spark that may return is a live
        # owner, so the switch keeps waiting for it.
        service = _service(
            sessions,
            NOW,
            lifecycle,
            RecordingArtifactExecutor(),
            artifacts=CompleteArtifactInspector(),
        )
        service._clock = lambda: now[0]
        assert waiting.result.final_verify_started_at is None
        now[0] = NOW + timedelta(hours=6)
        service.tick()
        assert service.get(operation.operation_id).state not in {
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
        # A revoked Spark never returns: the wait ends with its cause.
        with sessions.begin() as session:
            node = session.get(AgentNode, nodes[0])
            assert node is not None
            node.revoked_at = now[0]
        now[0] += timedelta(minutes=1)
        assert service._advance(operation.operation_id) is True
        ended = service.get(operation.operation_id)
        with sessions.begin() as session:
            node = session.get(AgentNode, nodes[0])
            assert node is not None
            node.state = active_state
            node.revoked_at = None
        from .non_blocking import assert_ended_without_blocking

        def cause(receipt):
            assert receipt.status_reason

        _, fresh = assert_ended_without_blocking(
            SimpleNamespace(sessions=sessions),
            operation,
            end=lambda _operation: ended,
            fresh=lambda _world: service.apply(
                RunSwitchApplyRequest(
                    **request.model_dump(), request_key=str(uuid.uuid4())
                ),
                actor="admin",
            ),
            assert_reason=cause,
            request_key=lambda receipt: receipt.request_key,
        )
        assert fresh.operation_id != operation.operation_id
        return

    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.state = active_state
    now[0] = due + timedelta(seconds=1)
    assert service._advance(operation.operation_id) is True
    resumed = service.get(operation.operation_id)
    assert resumed.state != LifecycleState.OBSERVING
    for _ in range(40):
        if not service._advance(operation.operation_id):
            break
    final = service.get(operation.operation_id)
    assert final.state not in {LifecycleState.OBSERVING, "failed"}, final.status_reason


def test_child_activity_change_persists_without_clock_only_writes(
    tmp_path: Path,
) -> None:
    """An unchanged poll is quiet, but a changed stall signal is durable."""

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    executor = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    request = _request(sessions, nodes[0])
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid.uuid4())),
        actor="admin",
    )
    for _ in range(8):
        assert service.tick() is True
        if _result(service.get(operation.operation_id)).child_operation_id is not None:
            break
    child = executor.children[_child_operation_id(service.get(operation.operation_id))]
    child = _replace_child(
        executor,
        _child_operation_id(service.get(operation.operation_id)),
        payload={
            "operation": {
                "phase": "transfer",
                "completed_bytes": 0,
                "total_bytes_known": False,
                "activity": "active",
                "observed_at": NOW.isoformat(),
            }
        },
    )
    assert service.tick() is True
    before = _result(service.get(operation.operation_id)).operation
    assert before is not None
    assert before.activity == "active"
    assert isinstance(child.result, RunSwitchDistributionChildResult)
    assert child.result.progress.operation is not None
    child.result.progress.operation = child.result.progress.operation.model_copy(
        update={"observed_at": (NOW + timedelta(seconds=1)).isoformat()}
    )
    assert service.tick() is False
    child.result.progress.operation = child.result.progress.operation.model_copy(
        update={"activity": "waiting"}
    )
    assert service.tick() is True
    after = _result(service.get(operation.operation_id)).operation
    assert after is not None
    assert after.activity == "waiting"


def test_due_scheduler_reaches_work_past_a_full_parked_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sixteen parked scopes must not starve a seventeenth due scope."""

    sessions, lifecycle, _queue, _mapping_id, _build_id, _nodes = setup_services(
        tmp_path
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    due_id = str(uuid.uuid4())
    with sessions.begin() as session:
        for index in range(17):
            job_id = f"00000000-0000-4000-8000-{index:012x}"
            session.add(
                Job(
                    id=job_id,
                    request_id=str(uuid.uuid4()),
                    kind="recipe.run-switch.v2",
                    state="running",
                    actor="admin",
                    authority_revision="a" * 64,
                    targets=[f"spk_{index:032x}"],
                    payload_digest="a" * 64,
                    payload={},
                    result={},
                    next_action_at=NOW if index == 16 else NOW + timedelta(minutes=1),
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            if index == 16:
                due_id = job_id
    seen: list[str] = []
    monkeypatch.setattr(service, "_advance", lambda job_id: seen.append(job_id) or True)
    assert service.tick() is True
    assert seen == [due_id]
    assert service.tick() is True
    assert seen == [due_id, due_id]


@pytest.mark.parametrize(
    ("damage", "expected_state"),
    [("plan", LifecycleState.FAILED), ("result", LifecycleState.RUNNING)],
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_malformed_operation_is_reconciled_without_aborting_the_batch(
    tmp_path: Path,
    damage: str,
    expected_state: LifecycleState,
) -> None:
    """One invalid persisted contract must not deny a valid operation its turn."""

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(child_transfer=True),
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    valid = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    malformed_id = "00000000-0000-4000-8000-000000000000"
    assert malformed_id < valid.operation_id
    with sessions.begin() as session:
        accepted = session.get(Job, valid.operation_id)
        assert accepted is not None
        payload = copy.deepcopy(accepted.payload)
        if damage == "plan":
            payload["plan"] = {"not": "a plan"}
        session.add(
            Job(
                id=malformed_id,
                request_id=str(uuid.uuid4()),
                kind="recipe.run-switch.v2",
                state="running",
                actor="admin",
                authority_revision="a" * 64,
                targets=[nodes[0]],
                payload_digest="a" * 64,
                payload=payload,
                result=["malformed"] if damage == "result" else None,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    with sessions() as session:
        stored_valid = session.get(Job, valid.operation_id)
        assert stored_valid is not None
        before_index = (stored_valid.result or {}).get("phase_index")

    assert service.tick() is True

    with sessions() as session:
        rejected = session.get(Job, malformed_id)
        assert rejected is not None
        assert rejected.state == expected_state
        if damage == "result":
            # Progress is reconstructed for fresh observation under the same
            # accepted request; no issued effect is declared stopped.
            assert rejected.state == LifecycleState.RUNNING
            assert rejected.result is not None
            restored = RunSwitchOperationResult.model_validate_json(
                json.dumps(rejected.result)
            )
            assert restored.force_replan
            assert rejected.next_action_at is not None
        advanced = session.get(Job, valid.operation_id)
        assert advanced is not None
        assert advanced.state == "running"
        after_index = (advanced.result or {}).get("phase_index")
    # The unrelated due operation still advanced in the same batch.
    assert after_index == 1
    assert after_index != before_index


def test_run_switch_admission_declares_the_resource_estimate_uncertain(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = RunSwitchOperationService(
        sessions,
        memory_floor_bytes=0,
        lifecycle=lifecycle,
        clock=lambda: lifecycle._clock(),
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    assert plan.allowed, plan.blockers
    node = plan.fit_current.nodes[0]
    assert node.resource_demand is not None
    assert node.resource_demand.total_bytes == node.memory_required_bytes


def test_mapping_selection_reads_typed_parameters_from_persisted_mapping(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, _build_id, _nodes = setup_services(
        tmp_path
    )
    with sessions.begin() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        assert mapping is not None
        mapping.parameters = {
            "engine_extension": {"enabled": False, "nullable": None},
        }

    with sessions() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        assert mapping is not None
        mapping_nodes = tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == mapping_id)
                .order_by(ClusterMappingNode.rank)
            )
        )
        selection = _service(
            sessions,
            lifecycle._clock(),
            lifecycle,
            RecordingArtifactExecutor(),
        )._mapping_selection(mapping, mapping_nodes)

    assert selection is not None
    assert selection.parameters == {
        "engine_extension": {"enabled": False, "nullable": None}
    }


def test_mapping_selection_rejects_malformed_persisted_parameters(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, _build_id, _nodes = setup_services(
        tmp_path
    )
    with sessions.begin() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        assert mapping is not None
        # Persisted JSON can be an array; the mapper must reject it rather than
        # treat it as an empty parameter map.
        session.execute(
            update(ClusterMapping)
            .where(ClusterMapping.id == mapping_id)
            .values(parameters=["malformed"])
        )

    with sessions() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        assert mapping is not None
        mapping_nodes = tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == mapping_id)
                .order_by(ClusterMappingNode.rank)
            )
        )
        with pytest.raises(Exception) as _ending:
            _service(
                sessions,
                lifecycle._clock(),
                lifecycle,
                RecordingArtifactExecutor(),
            )._mapping_selection(mapping, mapping_nodes)


def test_fresh_unmapped_group_uses_default_mapping_and_install_composite(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, _build_id, nodes = setup_services(tmp_path)
    node_id = nodes[0]
    with sessions.begin() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        assert mapping is not None
        for item in session.scalars(
            select(ClusterMappingNode).where(
                ClusterMappingNode.mapping_id == mapping_id
            )
        ):
            session.delete(item)
        session.delete(mapping)

    artifact_executor = RecordingArtifactExecutor()
    service = _service(
        sessions,
        lifecycle._clock(),
        lifecycle,
        artifact_executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    request = _request(sessions, node_id)
    plan = service.preview(request, actor="admin")

    assert plan.allowed is True
    assert plan.mapping is not None
    assert plan.mapping.action == "create"
    assert plan.build.state == "available"
    assert plan.preparation is not None
    # NAS readiness must not wait for copies on the Sparks. The preparation
    # below is exactly the one a profile preview and later apply will consume.
    assert plan.preparation.controller_ready is True
    assert plan.preparation.targets_ready is False

    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert operation.state == "queued"
    assert operation.operation_id
    assert (
        service.apply(
            RunSwitchApplyRequest(
                **request.model_dump(),
                plan_digest=plan.plan_digest,
                request_key=operation.request_key,
            ),
            actor="admin",
        ).operation_id
        == operation.operation_id
    )

    # The outcome: the default mapping is created and the install composite
    # is issued for it, whatever preparation precedes it.
    child = None
    for _ in range(10):
        assert service.tick() is True
        with sessions() as session:
            child = session.scalar(
                select(Job)
                .where(Job.kind == "recipe.install")
                .order_by(Job.created_at.desc())
            )
        if child is not None:
            break
    assert child is not None
    assert service.get(operation.operation_id).state == "running"
    with sessions() as session:
        created = session.scalar(
            select(ClusterMapping).where(
                ClusterMapping.placement_digest == plan.mapping.placement_digest
            )
        )
    assert created is not None


def test_model_cache_manifest_allows_planned_nas_download(tmp_path: Path) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: lifecycle._clock(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        model_cache=ModelCacheManifestProvider(missing_nas_bytes=1024),
        memory_floor_bytes=50,
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    assert plan.storage.nas_coverage == "partial"
    # The planned NAS download is allowed and fills the NAS before any copy
    # to a Spark reads it.
    assert plan.allowed, [reason.code for reason in plan.blockers]
    subphases = [phase.subphase for phase in plan.phases]
    assert subphases.index("model-download") < subphases.index("target-copy")
    assert plan.storage.missing_nas_bytes == 1024
    assert plan.preparation is not None
    expected_manifest = ModelCacheManifestProvider().resolve_artifact_set(
        model_content_sha256=plan.preparation.model.model_content_sha256
    )
    assert plan.preparation.model.artifact_set_sha256 == expected_manifest.digest
    assert plan.storage.artifact_set_sha256 == expected_manifest.digest
    assert plan.storage.artifact_set_bytes == plan.preparation.model.artifact_set_bytes


@pytest.mark.parametrize("missing", [False, True])
def test_profile_admits_unpublished_cache_observation(tmp_path, monkeypatch, missing):
    """A paused first receipt must wait in preparation, never refuse a load."""

    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    request = _request(sessions, nodes[0])
    manifest = ModelCacheManifestProvider().resolve_artifact_set(
        model_content_sha256=request.model_content_sha256
    )
    manifest = replace(
        manifest,
        artifacts=tuple(
            replace(spec, kind="huggingface.file") for spec in manifest.artifacts
        ),
    )
    cache = ModelCacheService(sessions, tmp_path / "cache", free_fraction=0)
    # Catalog resolution is not under test; storage and its observer are real.
    monkeypatch.setattr(cache, "resolve_artifact_set", lambda **_kwargs: manifest)
    entered, release = Event(), Event()
    stored_object = cache._stored_object

    def paused(digest, expected):
        entered.set()
        assert release.wait(30), "test did not release the first object observation"
        return stored_object(digest, expected)

    monkeypatch.setattr(cache, "_stored_object", paused)
    if not missing:
        spec = manifest.artifacts[0]
        path = cache._object_path(spec.sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * spec.expected_bytes)
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifact_phase_executor=RecordingArtifactExecutor(),
        model_cache=cache,
        memory_floor_bytes=50,
    )
    adapter = RunSwitchFleetProfileAdapter(sessions, service)
    profiles = FleetProfileService(
        sessions,
        clock=lifecycle._clock,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    with sessions() as session:
        revision = session.get(CatalogDocumentRevision, request.recipe_revision_id)
        assert revision is not None
        selector = f"{revision.publisher}/{revision.slug}"
    saved = profiles.create(
        FleetProfileInput(
            name="Observing",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector=selector, spark_ids=list(nodes)
                )
            ],
        ),
        actor="admin",
    )
    try:
        preview = profiles.preview(saved.id)
        assert entered.wait(30)
        assert preview.allowed, preview.reasons
        assert preview.preparations
        preparation = preview.preparations[0].preparation
        assert preparation.model.artifact_set_sha256 == manifest.digest
        assert not preparation.controller_ready
        assert all(
            reason.severity != ProfileReasonSeverity.ERROR for reason in preview.reasons
        )
        assessment = preview.assessments[0].assessment
        assert not assessment.blockers
        warning = next(
            reason
            for reason in assessment.warnings
            if reason.code == RunSwitchCode.NAS_COVERAGE_UNKNOWN
        )
        assert "0/1 objects checked" in warning.detail
        assert "next check" in warning.detail
        plan = profile_read(service.preview)(request, actor="admin")
        assert plan.allowed
        assert plan.storage.missing_nas_bytes is None
        phase = next(
            phase
            for phase in plan.phases
            if phase.subphase == ProfileChildPhase.MODEL_DOWNLOAD
        )
        executor = CompositeDistributionPhaseExecutor(
            sessions, None, None, model_cache=cache, clock=lifecycle._clock
        )
        phase_request = str(uuid.uuid4())
        try:
            with pytest.raises(BookkeepingUnknown) as waiting:
                executor.execute(
                    plan,
                    phase,
                    item_index=0,
                    actor="admin",
                    request_key=phase_request,
                    progress=RunSwitchOperationResult(),
                )
            assert waiting.value.typed_reason is WaitReason.OBSERVATION_UNAVAILABLE
            assert "next check" in str(waiting.value)
            release.set()
            cache._storage_observer._pending[manifest.digest].result(timeout=30)
            resumed = executor.execute(
                plan,
                phase,
                item_index=0,
                actor="admin",
                request_key=phase_request,
                progress=RunSwitchOperationResult(),
            )
            assert resumed.operation_id is not None
        finally:
            executor.close()
    finally:
        release.set()
        cache._storage_observer._pending[manifest.digest].result(timeout=30)
        cache.close()
    ready = profiles.preview(saved.id)
    assert ready.allowed
    assert ready.preparations[0].preparation.controller_ready is not missing
