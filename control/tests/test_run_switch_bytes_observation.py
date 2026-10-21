"""Observed bytes survive estimates through writers, SQL JSON and projections."""

import json
from pathlib import Path

import pytest
from vonk_agent_protocol import AgentProgress, LifecycleState, OperationProgress
from vonk_agent_protocol.agent_words import ProfileChildPhase, ProgressPhase
from vonk_control.fleet_profile_contract import FleetProfileSwitchAdapterResult
from vonk_control.run_switch_contract import (
    RunSwitchChildProgress,
    RunSwitchOperationResult,
)
from vonk_control.run_switch_observation_contract import RunSwitchObservedEvidence
from vonk_control.run_switch_operations import _persisted_result, _read_progress
from vonk_control.run_switch_progress import (
    _complete_operation_progress,
    _complete_phase_progress,
    _merge_progress_evidence,
    _progress_view,
)

from .test_recipe_operations import NOW, setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _request,
    _service,
)


@pytest.mark.parametrize(
    "writer",
    [
        "completed_bytes",
        "copied_bytes",
        "downloaded_bytes",
        "child",
        "phase",
        ProfileChildPhase.MODEL_DOWNLOAD,
        "operation",
        "projection",
    ],
)
def test_every_byte_writer_preserves_observation_above_plan(
    tmp_path: Path, writer: str
) -> None:
    """Catches stale totals failing persistence and completion hiding measured bytes."""
    sessions, lifecycle, _, _, _, nodes = setup_services(tmp_path)
    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    phase = next(
        phase
        for phase in plan.phases
        if phase.subphase == ProfileChildPhase.TARGET_COPY
    )
    progress = RunSwitchOperationResult(total_bytes=1024, total_bytes_known=True)
    if writer in {"completed_bytes", "copied_bytes", "downloaded_bytes", "child"}:
        observation = {writer: 2048, "total_bytes": 1024}
        if writer == "child":
            observation = {
                "progress": {
                    "completed_bytes": 2048,
                    "total_bytes": 1024,
                    "members": [
                        {
                            "node_id": nodes[0],
                            "state": LifecycleState.RUNNING,
                            "completed_bytes": 2048,
                            "total_bytes": 1024,
                        }
                    ],
                }
            }
        _merge_progress_evidence(
            progress,
            plan,
            phase,
            RunSwitchObservedEvidence.model_validate(observation),
            NOW,
        )
    else:
        progress.completed_bytes = 2048
        if writer in {"phase", ProfileChildPhase.MODEL_DOWNLOAD}:
            if writer == ProfileChildPhase.MODEL_DOWNLOAD:
                phase = phase.model_copy(
                    update={"subphase": ProfileChildPhase.MODEL_DOWNLOAD}
                )
            _complete_phase_progress(progress, plan, phase)
        elif writer == "operation":
            _complete_operation_progress(plan, progress)
    restored = _read_progress(json.loads(json.dumps(_persisted_result(progress))))
    view = _progress_view(plan, restored, LifecycleState.RUNNING, None)
    assert restored.completed_bytes == view.completed_bytes == 2048
    assert restored.total_bytes == view.total_bytes == 2048
    for member in view.members:
        if member.total_bytes is not None:
            assert member.total_bytes >= member.completed_bytes


def test_agent_progress_over_estimate_is_retained_instead_of_dropped() -> None:
    """Catches the agent mapper dropping an entire valid heartbeat measurement."""
    body = AgentProgress.model_validate_json(
        json.dumps(
            {
                "fence": "f605d979-777f-4cdb-b72c-40fe34963447",
                "progress": {
                    "phase": ProgressPhase.TRANSFER,
                    "completed_bytes": 2048,
                    "total_bytes": 1024,
                    "total_bytes_known": True,
                    "members": [
                        {
                            "member_id": "spark",
                            "phase": ProgressPhase.TRANSFER,
                            "completed_bytes": 2048,
                            "total_bytes": 1024,
                        }
                    ],
                },
            }
        )
    )
    assert body.progress is not None
    restored = OperationProgress.model_validate_json(body.progress.model_dump_json())
    assert restored.completed_bytes == restored.total_bytes == 2048
    assert (
        restored.members[0].completed_bytes == restored.members[0].total_bytes == 2048
    )


@pytest.mark.parametrize("over_estimate", [False, True])
def test_production_transfer_document_round_trips(over_estimate: bool) -> None:
    """Catches the 2026-10-21 transfer shape failing after the next sample grows.

    The GET captured the last persisted sample, before the failing write. The
    over-estimate vector injects one more observed byte at every nested meter.
    """
    document = json.loads(
        (Path(__file__).parent / "fixtures/run-switch-bytes-20261021.json").read_bytes()
    )
    progress = document["children"][0]["result"]["run_switch"]
    if over_estimate:
        for meter in [
            progress,
            progress["operation"],
            *progress["members"],
            *progress["operation"]["members"],
        ]:
            meter["completed_bytes"] = meter["total_bytes"] + 1
    result = FleetProfileSwitchAdapterResult.model_validate_json(json.dumps(document))
    child = result.children[0]
    assert child.result is not None
    restored = _read_progress(_persisted_result(child.result.run_switch))
    assert restored.child_operation_id == progress["child_operation_id"]
    assert restored.completed_bytes == 20579664308 + over_estimate
    assert restored.total_bytes == restored.completed_bytes


@pytest.mark.parametrize("total", [None, 0, 1024])
def test_child_measurement_normalizes_known_totals_and_preserves_unknown(
    total: int | None,
) -> None:
    """Catches child receipts rejecting observations or inventing an unknown total."""
    child = RunSwitchChildProgress.model_validate_json(
        json.dumps(
            {
                "completed_bytes": 2048,
                "total_bytes": total,
                "total_bytes_known": total is not None,
            }
        )
    )
    restored = RunSwitchChildProgress.model_validate_json(child.model_dump_json())
    assert restored.completed_bytes == 2048
    assert restored.total_bytes == (None if total is None else 2048)
    assert restored.total_bytes_known == (total is not None)
