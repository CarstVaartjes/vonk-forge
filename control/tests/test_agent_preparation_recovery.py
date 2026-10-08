"""Request-owned recovery budgets and the irreversible job effect boundary."""

from __future__ import annotations

from datetime import UTC, timedelta

from vonk_control.agent_jobs import AgentJobService
from vonk_control.lifecycle import State

from .runtime_identity_support import claim_agent
from .test_agent_jobs import (
    COMMIT,
    NODE_A,
    STOP_PAYLOAD,
    _restart_interrupted_result,
    job_state,
    parent,
)
from .test_agent_jobs import service as _service_fixture

agent_service = _service_fixture
from .test_agent_operation_lifecycle import KIND_STOP, _job_run_payload, _stored


# The stored attempt ordinal owns the budget; restarting a service cannot reset
# it. Wrong implementation caught: bounded backoff with unlimited redispatch.
def test_permanent_dependency_ends_its_request_and_fresh_request_is_claimable(
    agent_service,
) -> None:
    from vonk_agent_protocol import (
        AgentFailureKind,
        AgentResult,
        AgentResultState,
        FailureCode,
        OutcomeFailed,
        RecipeStopResult,
    )
    from vonk_control.recovery_policy import RecoveryPolicy

    jobs, sessions, clock = agent_service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    for ordinal in range(1, RecoveryPolicy().max_failures + 1):
        jobs = AgentJobService(sessions, clock=clock)
        claim = claim_agent(jobs, NODE_A, "serial-a")
        assert claim is not None
        jobs.record_result(
            AgentResult(
                fence=claim.fence,
                state=AgentResultState.FAILED,
                result=OutcomeFailed(
                    code=FailureCode.RUNTIME_OBSERVATION_UNAVAILABLE,
                    reason="runtime storage unavailable",
                    failure_kind=AgentFailureKind.TEMPORARY_DEPENDENCY,
                ),
            )
        )
        stored = _stored(sessions, operation.id)
        assert stored.current_attempt == ordinal
        if stored.next_action_at is not None:
            clock.now = stored.next_action_at.replace(tzinfo=UTC) + timedelta(seconds=1)
    ended = _stored(sessions, operation.id)
    assert ended.state == State.FAILED.value
    assert ended.next_action_at is None
    assert job_state(sessions, operation.parent_job_id).state == State.FAILED.value
    fresh = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    admitted = claim_agent(jobs, NODE_A, "serial-a")
    assert admitted is not None
    assert _stored(sessions, fresh.id).current_attempt == 1
    jobs.succeed(admitted, RecipeStopResult())
    assert _stored(sessions, fresh.id).state == State.SUCCEEDED.value


def test_job_preparation_retries_without_replaying_an_uncertain_job(
    agent_service,
) -> None:
    from vonk_agent_protocol import (
        AgentFailureKind,
        AgentResult,
        AgentResultState,
        FailureCode,
        FailureStage,
        OutcomeEvidence,
        OutcomeFailed,
    )

    jobs, sessions, clock = agent_service
    kind, payload = _job_run_payload()
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.FAILED,
            result=OutcomeFailed(
                code=FailureCode.RUNTIME_OBSERVATION_UNAVAILABLE,
                reason="preparation storage unavailable",
                failure_kind=AgentFailureKind.TEMPORARY_DEPENDENCY,
                evidence=OutcomeEvidence(
                    stage=FailureStage.MODEL_MATERIALIZATION.value
                ),
            ),
        )
    )
    scheduled = _stored(sessions, operation.id)
    assert scheduled.next_action_at is not None
    clock.now = scheduled.next_action_at.replace(tzinfo=UTC) + timedelta(seconds=1)
    next_claim = claim_agent(jobs, NODE_A, "serial-a")
    assert next_claim is not None and next_claim.fence != claim.fence
    # An executed job does not carry the preparation boundary; its uncertain
    # result must never become a second blind execute claim.
    jobs.record_result(_restart_interrupted_result(next_claim, kind))
    assert claim_agent(jobs, NODE_A, "serial-a") is None
    from vonk_control.lifecycle.core import OBSERVE_BUDGET

    for _ in range(OBSERVE_BUDGET + 2):
        retained = _stored(sessions, operation.id)
        if retained.state == State.FAILED.value:
            break
        assert retained.next_action_at is not None
        clock.now = retained.next_action_at.replace(tzinfo=UTC) + timedelta(seconds=1)
        jobs.reconcile_orders()
        assert claim_agent(jobs, NODE_A, "serial-a") is None
    assert _stored(sessions, operation.id).state == State.FAILED.value
    fresh = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    assert _stored(sessions, fresh.id).current_attempt == 1
