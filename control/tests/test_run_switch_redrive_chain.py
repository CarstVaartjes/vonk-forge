"""One redrive chains run-switch transitions that need no outside wait."""

from __future__ import annotations

import uuid
from datetime import timedelta

from vonk_control.models import Job
from vonk_control.persisted_waits import schedule_wait
from vonk_control.run_switch_contract import RunSwitchApplyRequest
from vonk_control.run_switch_operations import observation

from .test_run_switch_operations import (
    NOW,
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _child_operation_id,
    _replace_child,
    _request,
    _result,
    _service,
    setup_services,
)


def _waiting_on_transfer(tmp_path):
    """A switch whose transfer child is issued and still running."""
    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    executor = RecordingArtifactExecutor(child_transfer=True)
    service = _service(
        sessions,
        NOW,
        lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    operation = service.apply(
        RunSwitchApplyRequest(
            **_request(sessions, nodes[0]).model_dump(), request_key=str(uuid.uuid4())
        ),
        actor="admin",
    )
    for _ in range(8):
        service.redrive(operation.operation_id)
        if _result(service.get(operation.operation_id)).child_operation_id:
            break
    child_id = _child_operation_id(service.get(operation.operation_id))
    assert executor.calls.count("transfer") == 1
    return service, executor, operation.operation_id, child_id


def test_finished_child_and_next_phase_happen_in_one_redrive(tmp_path) -> None:
    """Catches one transition per worker rotation: the next phase waited a turn."""
    service, executor, operation_id, child_id = _waiting_on_transfer(tmp_path)
    _replace_child(executor, child_id, state="succeeded")

    assert service.redrive(operation_id) is True

    progress = _result(service.get(operation_id))
    assert "transfer" in progress.completed_phases
    assert "verify" in executor.calls  # the next phase ran in the same redrive
    assert executor.calls.count("transfer") == 1  # never re-issued
    assert progress.phase_index > 1  # the chain moved past the inline verify


def test_outstanding_child_is_observed_once_per_redrive(tmp_path, monkeypatch) -> None:
    """Re-observing an outstanding child is not a chain step."""
    service, _executor, operation_id, _child_id = _waiting_on_transfer(tmp_path)
    calls: list[str] = []
    advance = service._advance

    def counted(job_id: str) -> bool:
        calls.append(job_id)
        return advance(job_id)

    monkeypatch.setattr(service, "_advance", counted)
    service.redrive(operation_id)
    assert len(calls) == 1


def test_chain_is_bounded_and_stops_at_a_hold(tmp_path, monkeypatch) -> None:
    """A transition that keeps moving is capped; a future due time ends the chain."""
    service, _executor, operation_id, _child_id = _waiting_on_transfer(tmp_path)
    calls: list[int] = []

    def moving(job_id: str) -> bool:
        calls.append(1)
        with service._sessions.begin() as session:
            job = session.get(Job, job_id)
            assert job is not None
            # An inline transition: the checkpoint moves, no child is issued.
            job.result = {
                **job.result,
                "child_operation_id": None,
                "item_index": len(calls),
            }
        return True

    monkeypatch.setattr(service, "_advance", moving)
    assert service.redrive(operation_id) is True
    assert len(calls) == observation._CHAIN_LIMIT

    calls.clear()

    def holding(job_id: str) -> bool:
        calls.append(1)
        with service._sessions.begin() as session:
            job = session.get(Job, job_id)
            assert job is not None
            job.result = {**job.result, "child_operation_id": None, "item_index": 0}
            schedule_wait(job, NOW + timedelta(seconds=30))
        return True

    monkeypatch.setattr(service, "_advance", holding)
    assert service.redrive(operation_id) is True
    assert len(calls) == 1


def test_chain_starts_no_transition_after_its_time_budget(
    tmp_path, monkeypatch, frozen_pass_budgets
) -> None:
    """A slow inline transition leaves the rest of the chain to the next turn."""
    service, _executor, operation_id, _child_id = _waiting_on_transfer(tmp_path)
    calls: list[int] = []

    def slow(job_id: str) -> bool:
        calls.append(1)
        frozen_pass_budgets.advance(observation._CHAIN_BUDGET_SECONDS)
        with service._sessions.begin() as session:
            job = session.get(Job, job_id)
            assert job is not None
            job.result = {
                **job.result,
                "child_operation_id": None,
                "item_index": len(calls),
            }
        return True

    monkeypatch.setattr(service, "_advance", slow)
    assert service.redrive(operation_id) is True
    assert len(calls) == 1


def test_failed_transition_mid_chain_stays_consistent_and_resumes(
    tmp_path, monkeypatch
) -> None:
    """A raise after a committed step keeps that step; a restart issues no duplicate."""
    service, executor, operation_id, child_id = _waiting_on_transfer(tmp_path)
    _replace_child(executor, child_id, state="succeeded")
    execute = executor.execute

    def crash(plan, phase, **kwargs):
        if phase.kind == "verify":
            raise ConnectionError("injected verify fault")
        return execute(plan, phase, **kwargs)

    monkeypatch.setattr(executor, "execute", crash)
    assert service.redrive(operation_id) is True

    with service._sessions() as session:
        job = session.get(Job, operation_id)
        assert job is not None and job.state in {"queued", "running", "observing"}
        assert job.next_action_at is not None  # held with a retry, never stuck
        retry_at = job.next_action_at.replace(tzinfo=NOW.tzinfo)
    progress = _result(service.get(operation_id))
    assert "transfer" in progress.completed_phases
    assert progress.child_operation_id is None

    # A fresh worker on the same database resumes the committed checkpoint.
    monkeypatch.setattr(executor, "execute", execute)
    restarted = _service(
        service._sessions,
        retry_at,
        service._lifecycle,
        executor,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
    )
    assert restarted.redrive(operation_id) is True
    assert "verify" in executor.calls
    assert executor.calls.count("transfer") == 1
