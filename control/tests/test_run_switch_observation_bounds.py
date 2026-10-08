"""Bounded parent observation never turns missing evidence into a route effect."""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select
from vonk_agent_protocol import RouteState, RunState, RunSwitchCode
from vonk_control.job_documents import RunSwitchJobPayload
from vonk_control.models import Job, RecipeRun
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchFinalVerifyResult,
    RunSwitchOperationResult,
)
from vonk_control.run_switch_operations import PhaseExecution
from vonk_control.stored_json import read_row_column

from .non_blocking import assert_ended_without_blocking
from .test_run_switch_operations import (
    NOW,
    _ObservingLifecycle,
    _parked_start_switch,
    _request,
    _result,
)


def test_expired_observation_preserves_published_route_and_admits_new_operation(
    tmp_path,
):
    """Catches stale observation withdrawing a serving route and orphaning its parent."""
    service, operation, _start_index = _parked_start_switch(tmp_path, healthy=True)
    sessions = service._sessions
    with sessions.begin() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        parent = read_row_column(job, "payload")
        assert isinstance(parent, RunSwitchJobPayload)
        plan = parent.plan
        run = session.scalar(select(RecipeRun))
        assert run is not None
        run_id = run.id
        run.state = RunState.RUNNING
        run.route_state = RouteState.PUBLISHED
        run.route_error = None
        progress = read_row_column(job, "result")
        assert isinstance(progress, RunSwitchOperationResult)
        progress.phase_index = len(plan.phases) - 1
        progress.phase = plan.phases[-1].kind
        progress.subphase = None
        progress.child_operation_id = None
        progress.start_deadline = NOW - timedelta(seconds=1)
        progress.final_verify_started_at = (NOW - timedelta(seconds=901)).timestamp()
        job.result = progress.model_dump(mode="json")
        node_id = job.targets[0]

    class MissingObservation:
        def execute(self, plan, phase, **_kwargs):
            return PhaseExecution(
                waiting=True,
                result=RunSwitchFinalVerifyResult(
                    phase=phase.kind,
                    run_id=run_id,
                    final_verified=False,
                    state=RunState.RUNNING,
                    route_state=RouteState.PUBLISHED,
                    healthy=False,
                    ranks=[],
                ),
            )

    service._phase_executor = MissingObservation()
    service._clock = lambda: NOW

    def end(_receipt):
        assert service._advance(operation.operation_id)
        with sessions() as session:
            run = session.get(RecipeRun, run_id)
            assert run is not None
            assert run.state == RunState.RUNNING
            assert run.route_state == RouteState.PUBLISHED
            assert run.route_error is None
        return service.get(operation.operation_id)

    def typed_reason(receipt):
        assert _result(receipt).failure_code == RunSwitchCode.FINAL_VERIFICATION_TIMEOUT

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        operation,
        end=end,
        fresh=lambda _world: service.apply(
            RunSwitchApplyRequest(
                **_request(sessions, node_id).model_dump(),
                request_key=str(uuid.uuid4()),
            ),
            actor="admin",
        ),
        assert_reason=typed_reason,
        request_key=lambda receipt: receipt.request_key,
    )


def _pending_stop(tmp_path):
    from vonk_control.run_switch_contract import RunSwitchStopApplyRequest

    service, _parent, _index = _parked_start_switch(tmp_path, healthy=True)
    with service._sessions.begin() as session:
        run = session.scalar(select(RecipeRun))
        assert run is not None
        run_id = run.id
        run.route_state = RouteState.PUBLISHED
    operation = service.apply_stop(
        RunSwitchStopApplyRequest(run_id=run_id, request_key=str(uuid.uuid4())),
        actor="admin",
    )
    return service, operation, run_id


def test_unknown_stop_expires_across_restart_without_blocking_fresh_request(tmp_path):
    """Catches retryable observation exceptions escaping the stop's time bound."""
    from vonk_agent_protocol import LifecycleState
    from vonk_control.run_switch_operations import (
        RunSwitchOperationService,
        RunSwitchRetryLater,
    )
    from vonk_control.run_switch_operations.constants import (
        _FINAL_VERIFICATION_MAX_SECONDS,
    )

    service, operation, run_id = _pending_stop(tmp_path)
    sessions = service._sessions

    attempts = []

    class UnknownStop:
        def execute(self, plan, phase, **_kwargs):
            attempts.append(phase.kind)
            raise RunSwitchRetryLater(RunSwitchCode.STOP_VERIFICATION_PENDING)

    service._phase_executor = UnknownStop()
    service._clock = lambda: NOW
    assert service._advance(operation.operation_id)
    assert service.get(operation.operation_id).state == LifecycleState.RUNNING
    assert len(attempts) == 1
    # The persisted acceptance time is the bound: changing the cause or
    # restarting observation cannot buy another day of retrying the same clear.
    service = RunSwitchOperationService(
        sessions,
        lifecycle=service._lifecycle,
        artifacts=service._artifacts,
        phase_executor=UnknownStop(),
        memory_floor_bytes=0,
        clock=lambda: NOW + timedelta(seconds=_FINAL_VERIFICATION_MAX_SECONDS + 1),
    )

    def end(_receipt):
        assert service._advance(operation.operation_id)
        with sessions() as session:
            run = session.get(RecipeRun, run_id)
            assert run is not None
            assert run.route_state == RouteState.PUBLISHED
        return service.get(operation.operation_id)

    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        node_id = job.targets[0]
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        operation,
        end=end,
        fresh=lambda _world: service.apply(
            RunSwitchApplyRequest(
                **_request(sessions, node_id).model_dump(),
                request_key=str(uuid.uuid4()),
            ),
            actor="admin",
        ),
        assert_reason=lambda receipt: _assert_code(
            receipt, RunSwitchCode.FINAL_VERIFICATION_TIMEOUT
        ),
        request_key=lambda receipt: receipt.request_key,
    )


def _assert_code(receipt, code):
    assert _result(receipt).failure_code == code


def test_disappeared_stop_target_ends_without_blocking_fresh_request(tmp_path):
    """Catches a missing exact run being retried forever as unknown bookkeeping."""
    service, operation, run_id = _pending_stop(tmp_path)
    sessions = service._sessions
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        session.delete(run)
        job = session.get(Job, operation.operation_id)
        assert job is not None
        node_id = job.targets[0]

    def end(_receipt):
        assert service._advance(operation.operation_id)
        return service.get(operation.operation_id)

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        operation,
        end=end,
        fresh=lambda _world: service.apply(
            RunSwitchApplyRequest(
                **_request(sessions, node_id).model_dump(),
                request_key=str(uuid.uuid4()),
            ),
            actor="admin",
        ),
        assert_reason=lambda receipt: _assert_code(
            receipt, RunSwitchCode.STOP_TARGET_DISAPPEARED
        ),
        request_key=lambda receipt: receipt.request_key,
    )


def test_observed_stop_settles_lost_child_acknowledgement_and_admits_fresh(tmp_path):
    """Catches a parked stop child blocking even after its exact ranks stopped."""
    from vonk_agent_protocol import LifecycleState
    from vonk_agent_protocol.agent_words import ProfileChildPhase
    from vonk_control.models import RunNode
    from vonk_control.reservation_owners import release_dead_owner_reservations

    service, operation, run_id = _pending_stop(tmp_path)
    sessions = service._sessions
    lifecycle = service._lifecycle
    assert isinstance(lifecycle, _ObservingLifecycle)
    lifecycle._healthy = False
    assert operation.result is not None
    stop_plan = lifecycle.preview_stop(run_id)
    child = lifecycle.stop(
        run_id,
        plan_digest=stop_plan.plan_digest,
        actor="admin",
        request_id=str(uuid.uuid4()),
        workload_intent_ordinal=operation.result.workload_intent_ordinal,
    )
    with sessions.begin() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        parent = read_row_column(job, "payload")
        assert isinstance(parent, RunSwitchJobPayload)
        stop_index = next(
            phase.index
            for phase in parent.plan.phases
            if phase.kind == ProfileChildPhase.STOP
        )
        progress = read_row_column(job, "result")
        assert isinstance(progress, RunSwitchOperationResult)
        progress.phase_index = stop_index
        progress.phase = parent.plan.phases[stop_index].kind
        progress.child_operation_id = child.id
        progress.force_replan = False
        job.result = progress.model_dump(mode="json")
        job.state = LifecycleState.RUNNING
        parked = session.get(Job, child.id)
        assert parked is not None
        parked.state = LifecycleState.NEEDS_OPERATOR
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = RunState.STOPPED
        run.route_state = RouteState.WITHDRAWN
        for rank in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            rank.state = RunState.STOPPED
        release_dead_owner_reservations(session, NOW)
        node_id = job.targets[0]

    def end(_receipt):
        for _ in range(4):
            service._advance(operation.operation_id)
        return service.get(operation.operation_id)

    ended, _fresh = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        operation,
        end=end,
        fresh=lambda _world: service.apply(
            RunSwitchApplyRequest(
                **_request(sessions, node_id).model_dump(),
                request_key=str(uuid.uuid4()),
            ),
            actor="admin",
        ),
        assert_reason=lambda receipt: _assert_code(
            receipt, RunSwitchCode.FINAL_VERIFICATION_TIMEOUT
        ),
        request_key=lambda receipt: receipt.request_key,
    )
    assert ended.state == LifecycleState.SUCCEEDED
