"""Run/Switch bookkeeping mismatches reconcile; the refusals that stay still refuse."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models import Job, RecipeRun
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchOperationResult,
)
from vonk_control.run_switch_operations import (
    _load_plan,
    _progress_view,
    _stored_result,
)

from .test_recipe_operations import installed_recipe, setup_services
from .test_run_switch_operations import (
    NOW,
    RecordingArtifactExecutor,
    _request,
    _service,
)


def _accepted(tmp_path: Path):
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
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
    return SimpleNamespace(
        sessions=sessions,
        service=service,
        request=request,
        plan=plan,
        operation=operation,
    )


def _damage_plan(sessions, operation_id: str) -> None:
    with sessions.begin() as session:
        job = session.get(Job, operation_id)
        assert job is not None
        payload = dict(job.payload)
        payload["plan"] = {"not": "a plan"}
        job.payload = payload


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_cancel_completes_when_the_stored_plan_is_unreadable(tmp_path: Path) -> None:
    accepted = _accepted(tmp_path)
    _damage_plan(accepted.sessions, accepted.operation.operation_id)

    cancelled = accepted.service.cancel(
        accepted.operation.operation_id,
        actor="admin",
        request_key=str(uuid.uuid4()),
        reason="Keep the current profile",
    )

    assert cancelled.state == "cancelled"


def test_unreadable_stored_documents_retire_as_unknown_values(tmp_path: Path) -> None:
    assert _load_plan({"not": "a plan"}) is None
    assert _load_plan("broken") is None
    assert isinstance(_stored_result(["malformed"]), Residue)
    assert _stored_result(None) is None


def test_an_operation_without_any_recorded_target_still_has_a_progress_view() -> None:
    progress = _progress_view(
        None, RunSwitchOperationResult(), "failed", "damaged row", node_ids=[]
    )

    assert [member.state for member in progress.members] == ["unknown"]


def test_an_inspector_without_model_cache_binding_does_not_fail_composition(
    tmp_path: Path,
) -> None:
    service = _accepted(tmp_path).service
    service._artifacts = object()  # type: ignore[assignment]

    service.bind_model_cache(object())  # type: ignore[arg-type]


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_stop_preview_rebuilds_a_damaged_run_plan_from_its_installation(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    run_plan = lifecycle._run_admission.plan_run(
        installation.owner_id, "old", now=lifecycle._clock()
    )
    run_id = lifecycle._run_admission.accept_run(
        run_plan, actor="admin", now=lifecycle._clock()
    )
    with sessions.begin() as session:
        session.execute(
            update(RecipeRun).where(RecipeRun.id == run_id).values(state="running")
        )
        run = session.scalar(select(RecipeRun).where(RecipeRun.id == run_id))
        assert run is not None
        run.plan = {"not": "a run plan"}
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())

    stop = service.preview_stop(run_id, actor="admin")

    assert stop.action == "stop"


def test_a_stale_reviewed_plan_still_refuses_at_recheck(tmp_path: Path) -> None:
    accepted = _accepted(tmp_path)
    stale = accepted.request.model_copy(
        update={"recipe_revision_id": str(uuid.uuid4())}
    )

    with (
        accepted.sessions() as session,
        pytest.raises(Exception) as _ending,
    ):
        accepted.service.recheck_resources_in_session(session, stale, accepted.plan)


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_retry_without_a_readable_plan_ends_without_replay_and_fresh_apply_recovers(
    tmp_path: Path,
) -> None:
    accepted = _accepted(tmp_path)
    _damage_plan(accepted.sessions, accepted.operation.operation_id)

    with pytest.raises(Exception) as _ending:
        accepted.service.retry(
            accepted.operation.operation_id,
            request_key=str(uuid.uuid4()),
            actor="admin",
        )

    plan = accepted.service.preview(accepted.request, actor="admin")
    fresh = accepted.service.apply(
        RunSwitchApplyRequest(
            **accepted.request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert fresh.operation_id != accepted.operation.operation_id


def test_fresh_retry_keeps_verified_progress_but_has_independent_clocks(
    tmp_path: Path,
) -> None:
    """Catches a fresh authorized request inheriting an exhausted predecessor deadline."""
    from datetime import timedelta

    from vonk_agent_protocol import LifecycleState
    from vonk_control.strict_json import serialize_json_value

    accepted = _accepted(tmp_path)
    with accepted.sessions.begin() as session:
        job = session.get(Job, accepted.operation.operation_id)
        assert job is not None
        progress = RunSwitchOperationResult.model_validate_json(json.dumps(job.result))
        progress.retryable = True
        progress.recovery_deadline_at = NOW - timedelta(seconds=1)
        progress.observation_deadline_at = NOW - timedelta(seconds=1)
        progress.observation_due_at = NOW - timedelta(seconds=1)
        job.state = LifecycleState.FAILED
        job.result = serialize_json_value(progress)
    fresh = accepted.service.retry(
        accepted.operation.operation_id, actor="admin", request_key=str(uuid.uuid4())
    )
    assert fresh.operation_id != accepted.operation.operation_id
    assert fresh.result is not None
    assert fresh.result.recovery_deadline_at is None
    assert fresh.result.observation_deadline_at is None
    assert fresh.result.observation_due_at is None
    assert fresh.result.phase_results == progress.phase_results
