"""The agent-upgrade rollout decides through the lifecycle core.

A rollout is a composite over its orders: it follows them, never waits for an
operator, and heals every legacy shape left by an older Controller without
touching a live dispatch or an order that is running.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from vonk_agent_protocol import (
    AgentFailureKind,
    FailureCode,
    HelperErrorCode,
    LifecycleState,
)
from vonk_agent_protocol.contracts import AgentFailureResult, AgentUpgradePayload
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_upgrades import AgentUpgradeService
from vonk_control.lifecycle import (
    CancelRequested,
    Effect,
    Outcome,
    Reported,
    State,
    Tick,
    transition,
)
from vonk_control.lifecycle.agent_upgrade import (
    UNSUPPORTED_DISPATCH,
    AgentUpgradeAdapter,
)
from vonk_control.models import AgentOperation, Job, JobAttempt

from .test_agent_upgrades import (  # noqa: F401  (published_source is autouse)
    NODE_A,
    OLD_IDENTITY,
    PACKAGE_MODEL,
    Clock,
    _claim_upgrade,
    _record_delayed_worker_running,
    _rollout,
    published_source,
)

NOW = datetime(2026, 8, 27, tzinfo=UTC)


def _stored(session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    assert job is not None
    return job


def _job(state: str, **fields) -> Job:
    return Job(
        id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        kind="agent-upgrade",
        state=state,
        actor="a",
        authority_revision="r",
        targets=[],
        payload_digest="d",
        payload={},
        current_attempt=fields.pop("current_attempt", 0),
        created_at=NOW,
        updated_at=NOW,
        **fields,
    )


STORED = (
    "queued",
    "running",
    "waiting-for-operator",
    "succeeded",
    "failed",
    "cancelled",
)


@pytest.mark.parametrize("stored", STORED)
def test_every_stored_shape_adopts_and_no_event_waits_for_an_operator(stored) -> None:
    adapter = AgentUpgradeAdapter()
    row = adapter.adopt(_job(stored, current_attempt=1))
    events = (
        Tick(),
        CancelRequested("newer", "superseded"),
        Reported(Outcome.UNKNOWN),
        Reported(Outcome.FAILED, retryable=True),
        Reported(Outcome.DONE),
    )
    for event in events:
        decision = transition(row, event, adapter, NOW)
        assert (
            decision.row.state is not State.NEEDS_OPERATOR
            or stored == ("waiting-for-operator")
            and event is Tick
        )  # a legacy wait is re-evaluated, never produced
    assert adapter.actions(row) == ()
    assert not adapter.irreversible(row)


def test_a_cancel_of_a_rollout_completes_at_once() -> None:
    adapter = AgentUpgradeAdapter()
    row = adapter.adopt(_job("queued", current_attempt=0))
    decision = transition(row, CancelRequested("new"), adapter, NOW)
    assert decision.row.state is State.CANCELLED


def test_the_superseding_request_is_adopted_as_the_cancel_request() -> None:
    row = AgentUpgradeAdapter().adopt(_job("queued", result={"superseded_by": "x"}))
    assert row.cancel_requested and row.cancel_request_key == "x"
    assert row.effect in (Effect.NONE, Effect.ISSUED)


def test_a_legacy_waiting_rollout_is_healed_from_its_orders(tmp_path) -> None:
    sessions, _operations, upgrades, job = _rollout(tmp_path, "legacy-wait")
    with sessions.begin() as session:
        parent = _stored(session, job.id)
        parent.state = "waiting-for-operator"
    assert upgrades.heal_rollouts() is True
    with sessions() as session:
        parent = _stored(session, job.id)
        # The first order is still queued: the rollout shows queued, not a wait.
        assert parent.state == "queued"


def test_a_rollout_the_worker_failed_as_unsupported_runs_again(tmp_path) -> None:
    sessions, _operations, upgrades, job = _rollout(tmp_path, "legacy-failed")
    with sessions.begin() as session:
        parent = _stored(session, job.id)
        parent.state = "failed"
        parent.status_reason = UNSUPPORTED_DISPATCH
    assert upgrades.heal_rollouts() is True
    with sessions() as session:
        assert _stored(session, job.id).state == "queued"
    assert upgrades.heal_rollouts() is False  # idempotent


def test_a_failed_rollout_that_failed_for_a_real_reason_is_not_reopened(
    tmp_path,
) -> None:
    sessions, _operations, upgrades, job = _rollout(tmp_path, "real-failure")
    with sessions.begin() as session:
        parent = _stored(session, job.id)
        parent.state = "failed"
        parent.status_reason = "Spark x upgrade failed: refused"
    assert upgrades.heal_rollouts() is False
    with sessions() as session:
        assert _stored(session, job.id).state == "failed"


def test_a_live_legacy_dispatch_and_a_running_order_are_untouched(tmp_path) -> None:
    clock = Clock()
    sessions, operations, upgrades, job = _rollout(tmp_path, "live", clock=clock)
    _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    _record_delayed_worker_running(sessions, job.id, clock())
    assert upgrades.heal_rollouts() is False
    with sessions() as session:
        assert _stored(session, job.id).state == "running"
        operation = session.scalar(
            __import__("sqlalchemy")
            .select(AgentOperation)
            .where(AgentOperation.parent_job_id == job.id)
        )
        assert operation.state == "running" and operation.current_attempt == 1
        attempt = session.scalar(
            __import__("sqlalchemy")
            .select(JobAttempt)
            .where(JobAttempt.job_id == job.id)
        )
        assert attempt.state == "running"


def test_an_expired_legacy_dispatch_is_projected_from_its_orders(tmp_path) -> None:
    clock = Clock()
    sessions, operations, upgrades, job = _rollout(tmp_path, "lapsed", clock=clock)
    _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
    _record_delayed_worker_running(sessions, job.id, clock())
    clock.advance(seconds=3600)
    upgrades.heal_rollouts()
    with sessions() as session:
        # An order is still running (its own fence decides it): the rollout is
        # queued behind it instead of staying a stale dispatch.
        assert _stored(session, job.id).state == "queued"


def test_the_worker_tick_heals_rollouts(tmp_path) -> None:
    sessions, operations, _upgrades, job = _rollout(tmp_path, "tick")
    with sessions.begin() as session:
        _stored(session, job.id).state = "waiting-for-operator"
    assert operations.reconcile_orders() is True
    with sessions() as session:
        assert _stored(session, job.id).state == "queued"


def test_a_rollout_ends_through_the_core_and_a_terminal_row_absorbs_events() -> None:
    adapter = AgentUpgradeAdapter()
    job = _job("queued")
    adapter.succeed(job, NOW, reason="done")
    assert job.state == LifecycleState.SUCCEEDED
    adapter.fail(job, NOW, "late")
    assert job.state == "succeeded"  # a terminal row is never rewritten
    other = _job("waiting-for-operator")
    adapter.fail(other, NOW, "why")
    assert other.state == LifecycleState.FAILED
    fresh = _job(LifecycleState.QUEUED)
    assert adapter.adopt(fresh).state is State.QUEUED


def test_a_projection_never_writes_a_wait() -> None:
    adapter = AgentUpgradeAdapter()
    job = _job("waiting-for-operator")
    adapter.project(job, NOW, reason=None)
    assert job.state == "queued"
    assert replace(adapter.adopt(job)).state is State.QUEUED


def test_package_preparation_dependency_retries_same_package_across_restart_then_ends_and_admits_fresh(
    tmp_path,
):
    from .agent_fences import fenced_attempt, fenced_operation
    from .non_blocking import assert_ended_without_blocking

    clock = Clock()
    sessions, operations, upgrades, _job = _rollout(
        tmp_path, "preparation-budget", clock=clock
    )
    original_id = None
    original_package = None
    original_deadline = None
    for number in range(1, 3):
        claim = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
        stored = fenced_operation(sessions, claim)
        if original_id is None:
            original_id = stored.id
            original_package = AgentUpgradePayload.model_validate_json(
                json.dumps(stored.payload)
            ).package_sha256
        assert stored.id == original_id
        assert (
            AgentUpgradePayload.model_validate_json(
                json.dumps(stored.payload)
            ).package_sha256
            == original_package
        )
        assert fenced_attempt(sessions, claim).attempt == number
        operations._finish(
            claim,
            LifecycleState.FAILED.value,
            result=AgentFailureResult(
                status=LifecycleState.FAILED.value,
                reason="package preparation observation unavailable",
                error_code=FailureCode.AGENT_UPGRADE_FAILED.value,
                failure_kind=AgentFailureKind.TEMPORARY_DEPENDENCY,
                helper_error_code=HelperErrorCode.PACKAGE_PREPARATION_UNAVAILABLE.value,
                retry_after_seconds=2,
            ),
            reason=None,
        )
        with sessions() as session:
            pending = session.get(AgentOperation, stored.id)
            assert pending is not None and pending.recovery_deadline is not None
            if original_deadline is None:
                original_deadline = pending.recovery_deadline
            assert pending.recovery_deadline == original_deadline
        # Reconstruct the actual queue owner from persisted rows, retaining
        # request/package identity and its attempt-derived finite budget.
        operations = AgentJobService(sessions, clock=clock)
        upgrades = AgentUpgradeService(sessions, operations, clock=clock)
        operations.set_result_consumer(upgrades.consume_agent_result)
        clock.advance(seconds=960)
    with sessions() as session:
        pending = session.get(AgentOperation, original_id)
        assert pending is not None and pending.recovery_deadline is not None
        deadline = pending.recovery_deadline.replace(tzinfo=UTC)
    # The persistent time budget, including the package safety fences, ends
    # this request before the failure-count budget can be reached.
    clock.advance(seconds=int((deadline - clock()).total_seconds()))
    assert operations.claim(NODE_A, "serial-a", runtime_identity=OLD_IDENTITY) is None
    with sessions() as session:
        ended = session.get(AgentOperation, original_id)
        assert ended is not None
        assert ended.next_action_at is None
        assert ended.state == LifecycleState.FAILED.value

    def admit_fresh(_world: object) -> AgentOperation:
        plan = upgrades.preview(None, PACKAGE_MODEL)
        fresh_job = upgrades.apply(
            None,
            PACKAGE_MODEL,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=str(uuid.uuid4()),
        )
        fresh = _claim_upgrade(operations, NODE_A, "serial-a", OLD_IDENTITY)
        operation = fenced_operation(sessions, fresh)
        assert operation.id != original_id
        assert operation.parent_job_id == fresh_job.id
        return operation

    def assert_original_attempt(_operation: AgentOperation) -> None:
        assert fenced_attempt(sessions, claim).operation_id == original_id

    with sessions() as session:
        assert_ended_without_blocking(
            session,
            ended,
            end=lambda operation: operation,
            fresh=admit_fresh,
            assert_reason=assert_original_attempt,
            request_key=lambda operation: operation.id,
        )
