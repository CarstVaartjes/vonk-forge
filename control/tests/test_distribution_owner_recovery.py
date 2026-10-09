"""Durable distribution recovery through the accepted Run/Switch parent."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState
from vonk_agent_protocol.agent_words import ProfileChildPhase
from vonk_control.models import Job, NodeInventorySnapshot
from vonk_control.run_switch_contract import RunSwitchApplyRequest
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_run_switch_operations import (
    CompleteArtifactInspector,
    _cold_compile_switch,
    _finish,
    _request,
)


@pytest.mark.parametrize(
    "blocked_phase", [ProfileChildPhase.PREPARE, ProfileChildPhase.TRANSFER]
)
def test_distribution_observation_deadline_survives_restart_and_fresh_admission(
    tmp_path: Path,
    blocked_phase: ProfileChildPhase,
) -> None:
    """A source miss cannot retain an accepted parent's claims indefinitely."""
    switch = _cold_compile_switch(tmp_path, seconds=0, slow_compiles=0)
    execute = switch.executor.execute
    source_observed = []

    def unavailable(plan, phase, **kwargs):
        if phase.kind == blocked_phase:
            source_observed.append(True)
            raise RuntimeError("managed source unavailable")
        return execute(plan, phase, **kwargs)

    switch.executor.execute = unavailable
    for _ in range(30):
        switch.drive()
        view = switch.service.get(switch.operation.operation_id)
        if source_observed:
            break
    else:
        pytest.fail("accepted distribution never reached its source")
    deadline = view.result.recovery_deadline_at
    assert deadline is not None
    restarted = RunSwitchOperationService(
        switch.sessions,
        lifecycle=switch.lifecycle,
        clock=switch.clock,
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        phase_executor=switch.executor,
        artifact_phase_executor=switch.executor,
        memory_floor_bytes=50,
        inventory_max_age_seconds=86400,
    )
    restored = restarted.get(switch.operation.operation_id)
    assert restored.result is not None
    assert restored.result.recovery_deadline_at == deadline
    # A pre-Start source miss may request a fresh plan. Restart must preserve
    # its recovery checkpoint and bounded retry, rather than pin that strategy.
    assert restored.result == view.result
    assert restored.state == view.state
    assert restored.state in {LifecycleState.RUNNING, LifecycleState.OBSERVING}
    assert restored.next_attempt_at == view.next_attempt_at
    assert restored.next_attempt_at is not None
    assert switch.clock.now < restored.next_attempt_at <= deadline
    switch.clock.now = deadline
    restarted.tick()
    ended = restarted.get(switch.operation.operation_id)
    assert ended.result is not None
    assert not ended.result.retryable
    assert ended.result.child_operation_id is None
    assert ended.next_attempt_at is None
    observed_attempts = len(source_observed)
    for _ in range(3):
        switch.clock.now += timedelta(minutes=2)
        restarted.tick()
    assert len(source_observed) == observed_attempts
    switch.executor.execute = execute
    # The one-hour fault also ages inventory. Publish a new observation before
    # testing fresh admission so a distinct physical prerequisite is satisfied.
    with switch.sessions.begin() as session:
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.observed_at = switch.clock.now
    request = _request(switch.sessions, switch.nodes[0])
    plan = restarted.preview(request, actor="admin")
    assert plan.allowed
    fresh = restarted.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid4()),
        ),
        actor="admin",
    )
    assert fresh.operation_id != switch.operation.operation_id
    for _ in range(30):
        view = restarted.get(fresh.operation_id)
        assert view.result is not None
        due = view.next_attempt_at or view.result.observation_due_at
        if due is not None and due > switch.clock.now:
            switch.clock.now = due
        restarted.tick()
        view = restarted.get(fresh.operation_id)
        assert view.result is not None
        checkpoint = view.result.preflight
        if checkpoint is not None and checkpoint.pending_job_id:
            with switch.sessions() as session:
                pending = session.get(Job, checkpoint.pending_job_id)
                needs_result = pending is not None and pending.state in {
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                }
            if needs_result:
                _finish(switch.sessions, checkpoint, switch.clock.now)
        if ProfileChildPhase.TRANSFER in view.completed_phases:
            break
    else:
        pytest.fail(
            "fresh accepted request did not execute its distribution: "
            + view.model_dump_json()
        )
    assert ProfileChildPhase.TARGET_COPY in switch.executor.events
