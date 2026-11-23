"""Run/Switch operations: runtime probes."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt
from vonk_control.run_switch_contract.requests import (
    RunSwitchApplyRequest,
)

from .recipe_operations_support import NOW, setup_services
from .run_switch_operations_support import RecordingArtifactExecutor, _request, _service
from .test_lifecycle_preflight import _clear


def _probing_switch(tmp_path, *, probe=True, engine=None):
    """A load whose first phase probes its Spark; the probe stays unanswered."""
    sessions, lifecycle, queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path, engine=engine
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
    # No receipt is reusable, so the first phase issues a probe on the Spark.
    _clear(sessions)
    switch = SimpleNamespace(
        sessions=sessions,
        queue=queue,
        node=nodes[0],
        service=service,
        request=request,
        plan=plan,
        operation=operation,
    )
    if probe:
        for _ in range(4):
            if _probe_ids(switch):
                break
            service.tick()
    return switch


def _probe_ids(switch) -> tuple[str, str] | None:
    """The run-switch's issued probe: its child job and its Spark order."""
    from vonk_agent_protocol.contracts import AgentOperation as OperationKind

    with switch.sessions() as session:
        probe = session.scalar(
            select(AgentOperation).where(
                AgentOperation.kind == OperationKind.RUNTIME_PREFLIGHT.value
            )
        )
        return None if probe is None else (probe.parent_job_id, probe.id)


def _cancel(switch) -> None:
    switch.service.cancel(
        switch.operation.operation_id,
        actor="admin",
        request_key=str(uuid.uuid4()),
        reason="The operator chose another model",
    )
    for _ in range(3):
        if switch.service.get(switch.operation.operation_id).state == "cancelled":
            break
        switch.service.tick()
    assert switch.service.get(switch.operation.operation_id).state == "cancelled"


def _probe_states(switch, ids) -> tuple[str, str]:
    with switch.sessions() as session:
        job = session.get(Job, ids[0])
        probe = session.get(AgentOperation, ids[1])
        assert job is not None and probe is not None
        return job.state, probe.state


def _admits_a_fresh_load(switch) -> None:
    fresh = switch.service.apply(
        RunSwitchApplyRequest(
            **switch.request.model_dump(),
            plan_digest=switch.plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert fresh.state not in {"cancelled", "failed"}


def test_cancel_ends_the_queued_runtime_probe_with_its_run_switch(tmp_path):
    """Catches a cancelled run-switch leaving its unclaimed probe queued.

    Incident 2026-10-26: cancelled loads left their preflight children queued
    on a Spark whose claim lane a build held; they would have run (and held the
    lane) for an owner that no longer wanted them.
    """
    switch = _probing_switch(tmp_path)
    ids = _probe_ids(switch)
    assert ids is not None
    assert _probe_states(switch, ids)[1] == LifecycleState.QUEUED.value
    _cancel(switch)
    cancelled = LifecycleState.CANCELLED.value
    assert _probe_states(switch, ids) == (cancelled, cancelled)
    _admits_a_fresh_load(switch)


def test_cancel_while_the_probe_is_issued_still_ends_it(tmp_path):
    """Catches the probe a round commits after the cancel's terminal write.

    The probe is committed before the round saves its checkpoint; a cancel in
    between ended the run-switch from a checkpoint that did not name it.
    """
    switch = _probing_switch(tmp_path, probe=False)
    executor = switch.service._phase_executor
    issue = executor.preflight

    def cancel_after_issue(*arguments, **keywords):
        issued = issue(*arguments, **keywords)
        _cancel(switch)
        return issued

    executor.preflight = cancel_after_issue
    switch.service.tick()
    ids = _probe_ids(switch)
    assert ids is not None
    cancelled = LifecycleState.CANCELLED.value
    assert _probe_states(switch, ids) == (cancelled, cancelled)
    executor.preflight = issue
    _admits_a_fresh_load(switch)


def test_cancel_leaves_a_claimed_probe_to_report_its_own_end(tmp_path):
    """Catches a cancel overwriting the probe a Spark is running."""
    switch = _probing_switch(tmp_path)
    ids = _probe_ids(switch)
    assert ids is not None
    with switch.sessions.begin() as session:
        probe = session.get(AgentOperation, ids[1])
        assert probe is not None
        probe.state = LifecycleState.RUNNING.value
        probe.current_attempt = 1
        session.add(
            AgentOperationAttempt(
                operation_id=probe.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=NOW + timedelta(seconds=30),
                agent_certificate_serial="serial-0",
                state=LifecycleState.RUNNING.value,
            )
        )
    _cancel(switch)
    job_state, probe_state = _probe_states(switch, ids)
    assert probe_state == LifecycleState.RUNNING.value
    assert job_state != LifecycleState.CANCELLED.value
    _admits_a_fresh_load(switch)


def test_a_queued_probe_names_its_spark_holder_as_the_load_reason(tmp_path):
    """Catches the load reason staying at the reason its probe round started.

    Incident 2026-10-26: the load kept the reason its probe round started with
    for hours while its probe sat queued behind another order holding the Spark.
    """
    from vonk_agent_protocol.contracts import AgentOperation as OperationKind
    from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
    from vonk_control.agent_jobs.waits import refresh_queue_waits

    from .test_lifecycle_preflight import _heartbeat, _heartbeating_build

    sessions, lifecycle, _, _mapping_id, _build_id, nodes = setup_services(tmp_path)
    now = [NOW]
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
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
    _clear(sessions)
    for _ in range(4):
        view = service.get(operation.operation_id)
        if (
            view.result is not None
            and view.result.preflight is not None
            and (view.result.preflight.pending_jobs)
        ):
            break
        service.tick()
    holder = _heartbeating_build(
        sessions, nodes[0], now[0], kind=OperationKind.RECIPE_STOP
    )
    for _ in range(3):
        now[0] += timedelta(seconds=61)
        _heartbeat(sessions, holder, nodes[0], now[0])
        service.tick()
    with sessions.begin() as session:
        refresh_queue_waits(session, now[0], node_id=nodes[0])
    now[0] += timedelta(seconds=61)
    service.tick()

    view = service.get(operation.operation_id)
    assert view.status_reason == WaitReason.SPARK_BUSY
    assert view.wait is not None and view.wait.reason == WaitReason.SPARK_BUSY
    assert view.blockers[0].operation is not None
    assert view.blockers[0].operation.id == holder
    assert view.blockers[0].node_ids == [nodes[0]]
    # A fresh request for the same load is still admitted behind the wait.
    fresh = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert fresh.state not in {"cancelled", "failed"}


def test_a_live_probe_keeps_the_dependency_window_waiting(tmp_path):
    """Catches a switch ended as exhausted while its own probe is still live.

    A probe ends by its own report, the gone-target sweep, supersession or the
    parent's cancel, so the dependency window only runs while none is live.
    """
    from datetime import timedelta

    from vonk_control.run_switch_operations.observation_window import (
        DEPENDENCY_WINDOW,
        checkpoint_verdict,
    )
    from vonk_control.run_switch_operations.result_helpers import _read_progress
    from vonk_control.stall_window import Verdict

    switch = _probing_switch(tmp_path)
    assert _probe_ids(switch) is not None
    late = NOW + DEPENDENCY_WINDOW + timedelta(hours=1)
    with switch.sessions() as session:
        job = session.get(Job, switch.operation.operation_id)
        assert job is not None
        progress = _read_progress(job.result)
        assert checkpoint_verdict(job, progress, late) is Verdict.WAITING


@pytest.mark.parametrize("field", ["child_operation_id", "recovery_child_operation_id"])
def test_a_failed_run_switch_ends_its_unclaimed_probe_and_transfer(tmp_path, field):
    """Catches idempotent children outliving a run-switch that failed.

    The same class as the cancelled probe: a queued transfer child of an ended
    owner kept retrying for nobody, whether its phase child or the child a
    damaged checkpoint's recovery recorded. Copied bytes stay for reuse.
    """

    from vonk_control.lifecycle.run_switch import RunSwitchAdapter
    from vonk_control.lifecycle.types import State
    from vonk_control.run_switch_operations.result_helpers import _read_progress

    from .agent_jobs_support import distribution_payload

    switch = _probing_switch(tmp_path)
    probe = _probe_ids(switch)
    assert probe is not None
    with switch.sessions.begin() as session:
        transfer = Job(
            id=str(uuid.uuid4()),
            request_id=str(uuid.uuid4()),
            kind="artifact.distribution.v1",
            state=LifecycleState.QUEUED.value,
            actor="admin",
            authority_revision="a" * 64,
            targets=[switch.node],
            payload_digest="a" * 64,
            payload={"workload_intent_ordinal": 1},
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(transfer)
        session.flush()
        transfer_order = str(uuid.uuid4())
        switch.queue.enqueue_in_session(
            session,
            transfer.id,
            switch.node,
            "artifact.distribution.v1",
            "a" * 64,
            distribution_payload(),
            operation_id=transfer_order,
        )
        job = session.get(Job, switch.operation.operation_id)
        assert job is not None
        progress = _read_progress(job.result)
        setattr(progress, field, transfer.id)
        adapter = RunSwitchAdapter(session)
        before = adapter.lifecycle(job, progress, NOW)
        adapter.apply(job, progress, before, replace(before, state=State.FAILED), NOW)
    cancelled = LifecycleState.CANCELLED.value
    assert _probe_states(switch, probe) == (cancelled, cancelled)
    assert _probe_states(switch, (transfer.id, transfer_order)) == (
        cancelled,
        cancelled,
    )
    _admits_a_fresh_load(switch)


def test_postgres_a_probe_a_writer_held_at_the_end_is_ended_on_the_next_check(
    tmp_path, postgres_engine
):
    """Catches an ended owner leaving the probe another writer held queued.

    The abandon never waits for a lock: a probe a claim or result holds at the
    terminal write is skipped. The owner's terminal re-check offers it again
    until nothing it issued is left queued.
    """
    switch = _probing_switch(tmp_path, engine=postgres_engine)
    now = [NOW]
    switch.service._clock = lambda: now[0]
    ids = _probe_ids(switch)
    assert ids is not None
    holder = switch.sessions()
    try:
        holder.begin()
        holder.scalar(select(Job).where(Job.id == ids[0]).with_for_update())
        _cancel(switch)
        assert _probe_states(switch, ids)[1] == LifecycleState.QUEUED.value
    finally:
        holder.rollback()
        holder.close()
    # The worker re-drives the ended owner when its terminal wait is due.
    with switch.sessions() as session:
        ended = session.get(Job, switch.operation.operation_id)
        assert ended is not None and ended.next_action_at is not None
        now[0] = ended.next_action_at.replace(tzinfo=UTC)
    switch.service.redrive(switch.operation.operation_id)
    cancelled = LifecycleState.CANCELLED.value
    assert _probe_states(switch, ids) == (cancelled, cancelled)
    with switch.sessions() as session:
        ended = session.get(Job, switch.operation.operation_id)
        assert ended is not None and ended.next_action_at is None
    _admits_a_fresh_load(switch)
