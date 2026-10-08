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
from .test_run_switch_operations import NOW, _parked_start_switch, _request, _result


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
