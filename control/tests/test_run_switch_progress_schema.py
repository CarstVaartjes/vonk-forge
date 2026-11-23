"""Every progress writer survives the same persisted JSON reader and restart."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from vonk_agent_protocol import LifecycleState
from vonk_agent_protocol.agent_words import ProfileChildPhase, ProfileReportedPhase
from vonk_control.models import Job
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchOperationResult,
)
from vonk_control.run_switch_operations import _persisted_result, _read_progress

from .test_recipe_operations import NOW, setup_services
from .test_run_switch_operations import (
    ColdStartPhaseExecutor,
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _fixture_execution,
    _request,
    _service,
)


def test_writer_normalizes_a_mutated_measurement_before_persistence() -> None:
    """Catches an observation over an estimate failing the durable writer."""
    progress = RunSwitchOperationResult(total_bytes=0, total_bytes_known=True)
    progress.completed_bytes = 1
    restored = _read_progress(_persisted_result(progress))
    assert restored.completed_bytes == restored.total_bytes == 1


@pytest.mark.parametrize(
    "damage_phase",
    [
        None,
        ProfileChildPhase.PREPARE,
        ProfileChildPhase.TRANSFER,
        ProfileChildPhase.VERIFY,
        ProfileChildPhase.START,
        ProfileReportedPhase.FINAL_VERIFY,
    ],
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_every_phase_writer_round_trips_and_recovers_after_measurement_damage(
    tmp_path: Path, damage_phase: ProfileChildPhase | ProfileReportedPhase | None
) -> None:
    """Catches retiring unreadable progress and final verification schema drift.

    Each tick crosses apply/worker/SQL/reader, and a fresh service resumes the
    same request. Repeated prepare phases and the final-verify writer are read
    exactly as real decoded database JSON, rather than validated Python tuples.
    """
    sessions, lifecycle, _, _mapping, _build, nodes = setup_services(tmp_path)

    class PhaseWriter(ColdStartPhaseExecutor):
        def execute(self, plan, phase, **kwargs):
            if phase.kind in {
                ProfileChildPhase.START,
                ProfileReportedPhase.FINAL_VERIFY,
            }:
                return _fixture_execution(plan, phase, result={"phase": phase.kind})
            if phase.kind == ProfileChildPhase.CLEANUP:
                return RecordingArtifactExecutor().execute(plan, phase, **kwargs)
            return super().execute(plan, phase, **kwargs)

    executor = PhaseWriter()
    inspector = CompleteArtifactInspector(missing_spark_bytes=1024)
    service = _service(
        sessions, NOW, lifecycle, executor, artifacts=inspector, phase_executor=executor
    )
    request = _request(sessions, nodes[0])
    operation = service.apply(
        RunSwitchApplyRequest(**request.model_dump(), request_key=str(uuid4())),
        actor="admin",
    )
    injected = False
    seen = set()
    for _ in range(40):
        current = service.get(operation.operation_id)
        assert current.state != LifecycleState.FAILED, current.status_reason
        assert current.result is not None, current.model_dump_json()
        persisted = _persisted_result(current.result)
        assert _read_progress(json.loads(json.dumps(persisted))) == current.result
        if current.current_phase is not None:
            seen.add(current.current_phase)
        if current.state == LifecycleState.SUCCEEDED:
            break
        if not injected and current.current_phase == damage_phase:
            with sessions.begin() as session:
                row = session.get(Job, operation.operation_id)
                assert row is not None and row.result is not None
                row.result = {
                    **row.result,
                    "completed_bytes": 2,
                    "total_bytes": 1,
                    "total_bytes_known": True,
                }
            injected = True
        service = _service(
            sessions,
            NOW,
            lifecycle,
            executor,
            artifacts=inspector,
            phase_executor=executor,
        )
        service.tick()
    else:
        pytest.fail(service.get(operation.operation_id).model_dump_json())
    assert ProfileChildPhase.PREPARE in seen
    assert current.result.phase_index > 0
    # Some plans omit stop, uninstall and cleanup: damage only planned phases.
    assert injected or damage_phase not in seen


def test_production_queue_receipts_share_the_persisted_progress_contract() -> None:
    """Catches nested final verification or a nonzero queue index using another schema.

    Captured by GET on 2026-10-20: the failed run's invalid result is omitted
    by the API; the final verification belongs to a succeeding Stop at index 2.
    This fixture does not invent the unavailable raw failed document.
    """
    from vonk_control.fleet_profile_contract import FleetProfileSwitchAdapterResult

    result = FleetProfileSwitchAdapterResult.model_validate_json(
        (
            Path(__file__).parent / "fixtures/run-switch-hunyuan-profile-20261020.json"
        ).read_bytes()
    )
    failed = next(child for child in result.children if child.queue_index == 1)
    assert failed.operation_id == "5692264a-216f-4660-9482-0de9e19ca46e"
    assert failed.result is None
    stop = next(child for child in result.children if child.queue_index == 2)
    assert stop.result is not None
    progress = _read_progress(_persisted_result(stop.result.run_switch))
    assert progress == stop.result.run_switch
    assert progress.phase == ProfileReportedPhase.FINAL_VERIFY


def test_replan_replaces_measurements_owned_by_the_previous_transfer_budget(
    tmp_path: Path,
) -> None:
    """Catches a refreshed zero-copy plan retaining the old byte high-water mark."""
    from .test_run_switch_bookkeeping import _accepted

    accepted = _accepted(tmp_path)
    with accepted.sessions.begin() as session:
        row = session.get(Job, accepted.operation.operation_id)
        assert row is not None and row.result is not None
        progress = _read_progress(row.result)
        progress.completed_bytes = 1024
        progress.total_bytes = 2048
        progress.total_bytes_known = True
        progress.force_replan = True
        row.result = _persisted_result(progress)
    assert accepted.service._refresh_blocked_plan(accepted.operation.operation_id, NOW)
    current = accepted.service.get(accepted.operation.operation_id)
    assert current.result is not None
    assert current.result.total_bytes == 0
    assert current.result.completed_bytes == 0
    assert not current.result.force_replan


@pytest.mark.usefixtures("damaged_json_rows")
def test_unreadable_checkpoint_replans_from_current_observation(tmp_path: Path) -> None:
    """Catches unreadable checkpoints ending the accepted request instead of replanning."""
    from .test_run_switch_bookkeeping import _accepted

    accepted = _accepted(tmp_path)
    with accepted.sessions.begin() as session:
        row = session.get(Job, accepted.operation.operation_id)
        assert row is not None and row.result is not None
        row.result = ["unreadable checkpoint"]
    accepted.service._advance(accepted.operation.operation_id)
    recovered = accepted.service.get(accepted.operation.operation_id)
    assert recovered.result is not None and recovered.result.force_replan
    assert recovered.state != LifecycleState.FAILED
    assert recovered.next_attempt_at is not None
    accepted.service._advance(accepted.operation.operation_id)
    replanned = accepted.service.get(accepted.operation.operation_id)
    assert replanned.result is not None and not replanned.result.force_replan
    assert replanned.request_key == accepted.operation.request_key
    assert replanned.state == LifecycleState.QUEUED
