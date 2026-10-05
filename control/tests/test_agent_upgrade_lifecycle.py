"""The agent-upgrade rollout decides through the lifecycle core.

A rollout is a composite over its orders: it follows them, never waits for an
operator, and heals every legacy shape left by an older Controller without
touching a live dispatch or an order that is running.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
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
    assert job.state == "succeeded" and job.status_reason == "done"
    adapter.fail(job, NOW, "late")
    assert job.state == "succeeded"  # a terminal row is never rewritten
    other = _job("waiting-for-operator")
    adapter.fail(other, NOW, "why")
    assert other.state == "failed" and other.status_reason == "why"


def test_a_projection_never_writes_a_wait() -> None:
    adapter = AgentUpgradeAdapter()
    job = _job("waiting-for-operator")
    adapter.project(job, NOW, reason=None)
    assert job.state == "queued"
    assert replace(adapter.adopt(job)).state is State.QUEUED
