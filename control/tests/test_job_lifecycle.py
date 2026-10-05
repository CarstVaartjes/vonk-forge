"""The generic ``Job`` decides claim, lease, end and resume through the core."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.jobs import JobService, StaleAttempt
from vonk_control.lifecycle import Lifecycle, State, transition
from vonk_control.lifecycle.job import JobAdapter
from vonk_control.lifecycle.types import (
    CancelRequested,
    Claimed,
    Effect,
    Heartbeat,
    LeaseLapsed,
    Observed,
    OperatorAction,
    Outcome,
    Reported,
    Tick,
)
from vonk_control.models import Base, Job, JobAttempt

NOW = datetime(2026, 8, 3, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    clock = Clock()
    sessions = sessionmaker(engine, expire_on_commit=False)
    return JobService(sessions, clock=clock), sessions, clock


def _park(sessions, job_id: str, *, attempts: int = 1) -> None:
    with sessions.begin() as session:
        job = session.get(Job, job_id)
        job.state = "waiting-for-operator"
        job.current_attempt = attempts


def test_a_lapsed_lease_is_claimed_again_and_the_old_attempt_expires(env) -> None:
    jobs, sessions, clock = env
    job = jobs.enqueue("probe", "a", "r", ["n"], {})
    first = jobs.claim("w1", 30, kinds=("probe",))
    assert first is not None
    assert jobs.claim("w2", 30, kinds=("probe",)) is None  # a live lease is untouched
    clock.now += timedelta(seconds=31)
    second = jobs.claim("w2", 30, kinds=("probe",))
    assert second is not None and second.attempt == 2
    with sessions() as session:
        attempts = {a.attempt: a.state for a in session.scalars(select(JobAttempt))}
        assert attempts == {1: "expired", 2: "running"}
        assert session.get(Job, job.id).state == "running"
    with pytest.raises(StaleAttempt):
        jobs.succeed(first, {})  # the old fence is closed


def test_finish_ends_the_job_and_its_attempt_once(env) -> None:
    jobs, sessions, _ = env
    job = jobs.enqueue("probe", "a", "r", ["n"], {})
    fence = jobs.claim("w", 30, kinds=("probe",))
    assert fence is not None
    jobs.fail(fence, "boom")
    with sessions() as session:
        assert session.get(Job, job.id).state == "failed"
        assert session.scalars(select(JobAttempt.state)).one() == "failed"
    with pytest.raises(StaleAttempt):
        jobs.succeed(fence, {})
    with sessions() as session:
        assert session.get(Job, job.id).state == "failed"


def test_heartbeat_extends_only_the_current_fence(env) -> None:
    jobs, _sessions, clock = env
    jobs.enqueue("probe", "a", "r", ["n"], {})
    fence = jobs.claim("w", 30, kinds=("probe",))
    assert fence is not None
    clock.now += timedelta(seconds=20)
    renewed = jobs.heartbeat(fence, 30)
    assert renewed.lease_deadline == clock.now + timedelta(seconds=30)
    clock.now += timedelta(seconds=20)  # past the first lease, inside the second
    assert jobs.claim("w2", 30, kinds=("probe",)) is None


def test_a_legacy_waiting_job_adopts_as_a_wait_that_advertises_resume(env) -> None:
    jobs, sessions, _ = env
    job = jobs.enqueue("probe", "a", "r", ["n"], {})
    _park(sessions, job.id)
    adapter = JobAdapter()
    with sessions() as session:
        row = adapter.adopt(session.get(Job, job.id))
    assert row.state is State.NEEDS_OPERATOR
    assert adapter.actions(row) == ("resume",)
    # rule 1: a generic job is never irreversible, so the first decision retries it
    decision = transition(row, Tick(), adapter, NOW)
    assert decision.row.state is State.BACKOFF


def test_resume_queues_a_waiting_job_and_refuses_anything_else(env) -> None:
    jobs, sessions, _ = env
    job = jobs.enqueue("probe", "a", "r", ["n"], {})
    adapter = JobAdapter()
    with sessions.begin() as session:
        assert not adapter.resume(session, session.get(Job, job.id), NOW)  # queued
    _park(sessions, job.id)
    with sessions.begin() as session:
        stored = session.get(Job, job.id)
        stored.status_reason = "parked"
        assert adapter.resume(session, stored, NOW)
    with sessions() as session:
        stored = session.get(Job, job.id)
        assert (stored.state, stored.status_reason) == ("queued", None)
    assert jobs.claim("w", 30, kinds=("probe",)) is not None


def test_a_parked_job_with_a_lapsed_attempt_heals_by_resume_then_claim(env) -> None:
    jobs, sessions, clock = env
    job = jobs.enqueue("probe", "a", "r", ["n"], {})
    assert jobs.claim("w1", 30, kinds=("probe",)) is not None
    _park(sessions, job.id)
    with sessions.begin() as session:
        assert JobAdapter().resume(session, session.get(Job, job.id), clock.now)
    second = jobs.claim("w2", 30, kinds=("probe",))
    assert second is not None and second.attempt == 2


def test_a_running_job_with_a_live_lease_is_left_alone(env) -> None:
    jobs, sessions, _ = env
    job = jobs.enqueue("probe", "a", "r", ["n"], {})
    assert jobs.claim("w", 30, kinds=("probe",)) is not None
    adapter = JobAdapter()
    with sessions() as session:
        stored = session.get(Job, job.id)
        attempt = session.scalars(select(JobAttempt)).one()
        row = adapter.lifecycle(stored, attempt)
    assert row.state is State.RUNNING
    after = transition(row, Tick(), adapter, NOW).row
    assert (after.state, after.fence, after.attempt) == (State.RUNNING, row.fence, 1)


def test_no_event_leaves_a_generic_job_waiting_for_an_operator() -> None:
    adapter = JobAdapter()
    events = (
        Tick(),
        LeaseLapsed(),
        CancelRequested(),
        Observed(Effect.UNKNOWN),
        Reported(Outcome.UNKNOWN),
        Reported(Outcome.FAILED, retryable=True),
        Claimed(2, "f", NOW),
        Heartbeat(None, NOW),
        OperatorAction("resume"),
    )
    for state in State:
        for attempt in (0, 1, 3):
            for effect in Effect:
                row = Lifecycle(
                    id="j", kind="job", state=state, attempt=attempt, effect=effect
                )
                for event in events:
                    after = transition(row, event, adapter, NOW).row
                    if state is not State.NEEDS_OPERATOR:
                        assert after.state is not State.NEEDS_OPERATOR, (
                            state,
                            event,
                        )


def test_evidence_can_fail_an_ended_job_but_nothing_else_is_rewritten(env) -> None:
    jobs, sessions, _ = env
    job = jobs.enqueue("probe", "a", "r", ["n"], {})
    with sessions.begin() as session:
        stored = session.get(Job, job.id)
        assert not JobAdapter.amend_ended(stored, "x", NOW)  # still queued
        stored.state = "succeeded"
        assert JobAdapter.amend_ended(stored, "a member failed", NOW)
        assert (stored.state, stored.status_reason) == ("failed", "a member failed")
        assert not JobAdapter.amend_ended(stored, "again", NOW)


def test_new_job_is_the_one_constructor(env) -> None:
    jobs, sessions, _ = env
    made = JobAdapter.new_job(
        state="running",
        request_id="r1",
        kind="probe",
        actor="a",
        authority_revision="r",
        targets=["n"],
        payload_digest="d" * 64,
        payload={},
        current_attempt=0,
        created_at=NOW,
        updated_at=NOW,
    )
    with sessions.begin() as session:
        session.add(made)
    assert jobs.get(made.id).state == "running"
