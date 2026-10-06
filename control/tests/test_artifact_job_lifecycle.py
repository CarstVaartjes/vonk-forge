"""The one-shot artifact job on the shared lifecycle core.

An artifact job is the projection of its ``recipe.job.run.v1`` order.  This module
proves the adapter against the stored rows: what a stored (and a legacy) job *is*
to the core, that it follows its order (so no state is duplicated), that Stop is
its single operator action and never an empty wait, that a cancel always
completes (an unconfirmed stop ends ``cancelled`` with the effect unknown and a
residue record), that a legacy row adopts and heals, and that a restart in the
middle of a cancel is harmless.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from vonk_control import artifact_job_states as ajs
from vonk_control.agent_jobs import AgentJobService
from vonk_control.artifact_jobs import ArtifactJobResponse
from vonk_control.lifecycle import Effect, Lifecycle, State
from vonk_control.lifecycle.artifact_job import ArtifactJobAdapter
from vonk_control.models import AgentOperation, AgentOperationAttempt, ArtifactJob, Job

from .runtime_identity_support import claim_agent
from .test_artifact_jobs import (
    NOW,
    MutableClock,
    cancellation_result,
    running_artifact_service,
    submitted_artifact_job,
)

CANCEL_KEY = "00000000-0000-4000-8000-000000000301"
OTHER_KEY = "00000000-0000-4000-8000-000000000302"


def _issued_job(tmp_path, suffix: int):
    """A submitted job whose order a Spark claimed (so its effect may exist)."""

    sessions, operations, _queue, service, run_id, node_id = running_artifact_service(
        tmp_path
    )
    clock = MutableClock(NOW)
    agent_jobs = AgentJobService(sessions, clock=clock)

    def consume(session, operation, attempt, message) -> None:
        service.consume_agent_result(session, operation, attempt, message)
        operations.consume_agent_result(session, operation, attempt, message)

    agent_jobs.set_result_consumer(consume)
    operations._agent_jobs = agent_jobs
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    submitted = submitted_artifact_job(service, run_id, request_suffix=suffix)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    return sessions, operations, service, agent_jobs, clock, submitted, claim, run_id


def _cancelling(view) -> bool:
    """A cancel was requested and the job has not ended: it completes by itself."""

    return view.cancel_requested_at is not None and view.state not in ajs.ENDED


def _drive(service, agent_jobs, clock, job_id: str, *, until: str) -> str:
    """Advance the clock and reconcile until the job reaches ``until``."""

    for _ in range(60):
        state = service.get(job_id).state
        if state == until:
            return state
        clock.advance(seconds=120)
        agent_jobs.reconcile_orders()
    return service.get(job_id).state


def _write_legacy_wait(sessions, job_id: str, *, cancel_requested: bool) -> None:
    """The rows the pre-core writers left for a job waiting for an operator."""

    with sessions.begin() as session:
        job = session.get(ArtifactJob, job_id)
        assert job is not None and job.operation_id is not None
        job.state = "waiting-for-operator"
        job.status_reason = "artifact cancellation could not safely stop the scope"
        job.result_evidence = {
            "failure_kind": "cancellation-stop-uncertain",
            "recoverable": True,
            "active_scope_may_remain": True,
        }
        parent = session.get(Job, job.operation_id)
        assert parent is not None
        parent.state = "waiting-for-operator"
        if cancel_requested:
            parent.result = {
                **dict(parent.result or {}),
                "cancel_requested": True,
                "cancel_request_id": CANCEL_KEY,
                "cancel_requested_at": NOW.isoformat(),
            }
        order = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == parent.id)
        )
        assert order is not None
        order.state = "waiting-for-operator"
        order.next_action_at = None
        order.observe_count = 0
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == order.id,
                AgentOperationAttempt.attempt == order.current_attempt,
            )
        )
        assert attempt is not None
        attempt.state = "waiting-for-operator"


def _adopted(sessions, service, job_id: str):
    adapter = ArtifactJobAdapter(sessions=sessions, clock=lambda: NOW)
    with sessions() as session:
        job = session.get(ArtifactJob, job_id)
        assert job is not None
        return adapter.adopt(job), service.get(job_id)


# ------------------------------------------------------------------ adopt


def test_stored_jobs_map_onto_the_core(tmp_path) -> None:
    sessions, _ops, service, _agent_jobs, _clock, submitted, _claim, _run = _issued_job(
        tmp_path, 310
    )
    row, view = _adopted(sessions, service, submitted.id)
    # A queued job whose order a Spark holds is running work with a live lease.
    assert view.state == "running"
    assert row.attempt == 1
    assert row.lease_deadline is not None
    assert row.effect in {Effect.ISSUED, Effect.UNKNOWN}
    assert row.kind == "artifact-job"
    adapter = ArtifactJobAdapter(sessions=sessions, clock=lambda: NOW)
    assert adapter.irreversible(row) is True
    assert adapter.actions(row) == ("stop",)


def test_unsubmitted_jobs_have_nothing_to_stop(tmp_path) -> None:
    sessions, _ops, _queue, service, run_id, _node = running_artifact_service(tmp_path)
    from .test_artifact_jobs import artifact_create_request

    draft = service.create(
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000311")
    )
    row, view = _adopted(sessions, service, draft.id)
    assert (row.state, row.attempt, row.effect) == (State.QUEUED, 0, Effect.NONE)
    assert view.preparation == "draft"
    assert view.supported_actions == ()
    cancelled = service.cancel(
        draft.id,
        actor="operator",
        request_id=CANCEL_KEY,
        reason="not wanted",
    )
    # The core ends work that never ran at once, and it never waits for anyone.
    assert cancelled.state == "cancelled"
    assert cancelled.supported_actions == ()


# ------------------------------------------------------ never wait empty


def test_waiting_for_operator_must_advertise_stop(tmp_path) -> None:
    """The contract refuses an operator wait that offers no action."""

    _s, _o, service, _a, _c, submitted, _claim, _run = _issued_job(tmp_path, 305)
    base = ArtifactJobResponse.model_validate(
        service.get(submitted.id), from_attributes=True
    ).model_dump(mode="python")
    waiting = {
        **base,
        "state": "waiting-for-operator",
        "status_reason": "the job's effect is unknown",
    }
    with pytest.raises(ValueError, match="stop action"):
        ArtifactJobResponse.model_validate({**waiting, "supported_actions": ()})
    ok = ArtifactJobResponse.model_validate({**waiting, "supported_actions": ("stop",)})
    assert ok.supported_actions == ("stop",)


def test_a_lapsed_job_waits_only_with_stop_and_stop_completes_it(tmp_path) -> None:
    _sessions, _ops, service, agent_jobs, clock, submitted, _claim, _run = _issued_job(
        tmp_path, 320
    )
    clock.advance(seconds=31)
    agent_jobs.reconcile_orders()
    # The agent can no longer report: observed first, never a bare wait.
    state = _drive(service, agent_jobs, clock, submitted.id, until=ajs.NEEDS_OPERATOR)
    waiting = service.get(submitted.id)
    assert state == ajs.NEEDS_OPERATOR
    assert waiting.supported_actions == ("stop",)

    service.cancel(
        submitted.id, actor="operator", request_id=CANCEL_KEY, reason="lost job"
    )
    assert _cancelling(service.get(submitted.id))
    assert _drive(service, agent_jobs, clock, submitted.id, until="cancelled") == (
        "cancelled"
    )
    ended = service.get(submitted.id)
    assert ended.supported_actions == ()
    assert ended.result_evidence is not None
    assert ended.result_evidence.active_scope_may_remain is True


# ------------------------------------------------------- legacy adoption


def test_a_legacy_waiting_job_with_a_cancel_heals_to_cancelled(tmp_path) -> None:
    sessions, _ops, service, agent_jobs, clock, submitted, _claim, _run = _issued_job(
        tmp_path, 330
    )
    _write_legacy_wait(sessions, submitted.id, cancel_requested=True)
    row, view = _adopted(sessions, service, submitted.id)
    assert row.state is State.NEEDS_OPERATOR
    assert row.cancel_requested
    # Nobody has to act on it: the legacy label reads as the cancel it is.
    assert _cancelling(view)
    assert view.supported_actions == ()
    assert _drive(service, agent_jobs, clock, submitted.id, until="cancelled") == (
        "cancelled"
    )
    ended = service.get(submitted.id)
    assert ended.result_evidence is not None
    assert ended.result_evidence.active_scope_may_remain is True


def test_a_legacy_waiting_job_without_a_cancel_gets_the_stop_action(tmp_path) -> None:
    """The audit's C8: a waiting job used to be uncancellable."""

    sessions, _ops, service, agent_jobs, clock, submitted, _claim, _run = _issued_job(
        tmp_path, 340
    )
    _write_legacy_wait(sessions, submitted.id, cancel_requested=False)
    view = service.get(submitted.id)
    assert view.state == ajs.NEEDS_OPERATOR
    assert view.supported_actions == ("stop",)
    service.cancel(
        submitted.id, actor="operator", request_id=OTHER_KEY, reason="stop it"
    )
    assert _drive(service, agent_jobs, clock, submitted.id, until="cancelled") == (
        "cancelled"
    )


def test_a_running_job_with_a_live_lease_is_left_alone(tmp_path) -> None:
    sessions, _ops, service, agent_jobs, _clock, submitted, _claim, _run = _issued_job(
        tmp_path, 350
    )
    with sessions() as session:
        before = session.scalar(
            select(AgentOperationAttempt.lease_deadline).where(
                AgentOperationAttempt.operation_id
                == session.scalar(
                    select(AgentOperation.id).where(
                        AgentOperation.parent_job_id == submitted.operation_id
                    )
                )
            )
        )
    for _ in range(3):
        agent_jobs.reconcile_orders()
    assert service.get(submitted.id).state == "running"
    with sessions() as session:
        order = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == submitted.operation_id
            )
        )
        assert order is not None and order.state == "running"
        after = session.scalar(
            select(AgentOperationAttempt.lease_deadline).where(
                AgentOperationAttempt.operation_id == order.id
            )
        )
    assert after == before


# ------------------------------------------------------------ restart


def test_a_restart_in_the_middle_of_a_cancel_is_harmless(tmp_path) -> None:
    sessions, ops, service, agent_jobs, clock, submitted, claim, _run = _issued_job(
        tmp_path, 360
    )
    service.cancel(
        submitted.id, actor="operator", request_id=CANCEL_KEY, reason="stop it"
    )
    agent_jobs.record_result(
        cancellation_result(
            claim, submitted, state="waiting-for-operator", reason="could not stop"
        )
    )
    assert _cancelling(service.get(submitted.id))
    # The Controller restarts: new services over the same database, repeated passes.
    restarted = AgentJobService(sessions, clock=clock)
    ops._agent_jobs = restarted
    for _ in range(2):
        restarted.reconcile_orders()
    assert _cancelling(service.get(submitted.id))
    assert _drive(service, restarted, clock, submitted.id, until="cancelled") == (
        "cancelled"
    )
    again = service.get(submitted.id)
    restarted.reconcile_orders()
    restarted.reconcile_orders()
    assert service.get(submitted.id) == again  # a repeated pass changes nothing


def test_the_job_follows_its_order_without_a_second_state(tmp_path) -> None:
    """Whatever ends the order ends the job: there is no duplicate to drift."""

    sessions, _ops, service, agent_jobs, _clock, submitted, claim, _run = _issued_job(
        tmp_path, 370
    )
    service.cancel(
        submitted.id, actor="operator", request_id=CANCEL_KEY, reason="stop it"
    )
    agent_jobs.record_result(
        cancellation_result(
            claim, submitted, state="cancelled", reason="controller cancellation"
        )
    )
    ended = service.get(submitted.id)
    assert ended.state == "cancelled"
    # The agent receipted the cancel: stopped, so no residue is recorded.
    assert ended.result_evidence is not None
    assert ended.result_evidence.active_scope_may_remain is None
    with sessions() as session:
        order = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == submitted.operation_id
            )
        )
        assert order is not None and order.state == "cancelled"


def test_a_legacy_failed_lease_expiry_no_longer_blocks_the_runs_stop(tmp_path) -> None:
    """Before the core, a lapsed job ended ``failed`` and blocked Stop forever."""

    (
        sessions,
        operations,
        service,
        _agent_jobs,
        _clock,
        submitted,
        _claim,
        run_id,
    ) = _issued_job(tmp_path, 380)
    with sessions.begin() as session:
        job = session.get(ArtifactJob, submitted.id)
        assert job is not None and job.operation_id is not None
        job.state = "failed"
        job.status_reason = "artifact job agent lease expired"
        job.result_evidence = {
            "failure_kind": "agent-lease-expired",
            "recoverable": True,
            "late_results_accepted": False,
        }
        parent = session.get(Job, job.operation_id)
        assert parent is not None
        parent.state = "failed"
        order = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == parent.id)
        )
        assert order is not None
        order.state = "failed"
    plan = operations.preview_stop(run_id)
    stopped = operations.stop(
        run_id,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000399",
    )
    assert stopped.state == "succeeded"
    assert service.get(submitted.id).state == "failed"


@pytest.mark.parametrize("state", list(State))
@pytest.mark.parametrize("attempt", [0, 1, 3])
@pytest.mark.parametrize("cancelled", [False, True])
def test_no_stored_job_state_waits_without_an_action(state, attempt, cancelled) -> None:
    """The class guard: every core row that projects onto ``waiting-for-operator``
    advertises ``stop``, and every other row projects onto a state that needs
    nobody (it completes by itself)."""

    from dataclasses import replace

    row = Lifecycle(id="job", kind="artifact-job", state=state, attempt=attempt)
    if cancelled:
        row = replace(row, cancel_requested_at=NOW)
    job = ArtifactJob(state="queued", operation_id="op")
    adapter = ArtifactJobAdapter(object())  # type: ignore[arg-type]
    stored = adapter._stored_state(job, row)
    if stored == ajs.NEEDS_OPERATOR:
        assert adapter.actions(row) == ("stop",)
        assert attempt > 0 and not cancelled
        assert state is State.NEEDS_OPERATOR


def test_an_exact_stop_receipt_resolves_a_recorded_residue(tmp_path) -> None:
    sessions, _ops, service, agent_jobs, clock, submitted, claim, _run = _issued_job(
        tmp_path, 390
    )
    service.cancel(
        submitted.id, actor="operator", request_id=CANCEL_KEY, reason="stop it"
    )
    agent_jobs.record_result(
        cancellation_result(
            claim, submitted, state="waiting-for-operator", reason="could not stop"
        )
    )
    assert _drive(service, agent_jobs, clock, submitted.id, until="cancelled") == (
        "cancelled"
    )
    assert service.get(submitted.id).result_evidence.active_scope_may_remain is True  # type: ignore[index]
    with sessions.begin() as session:
        job = session.get(ArtifactJob, submitted.id)
        assert job is not None
        assert ArtifactJobAdapter(session).confirm_stopped(job, "runtime stopped", NOW)
        # Resolving twice, or an ended job without residue, changes nothing more.
        assert not ArtifactJobAdapter(session).confirm_stopped(
            job, "runtime stopped", NOW
        )
    resolved = service.get(submitted.id)
    assert resolved.state == "cancelled"
    assert resolved.result_evidence is not None
    assert resolved.result_evidence.active_scope_may_remain is False


# ------------------------------------ the stored vocabulary is the core's


def test_rows_written_before_the_rename_read_as_preparation_state_and_cancel(
    tmp_path,
) -> None:
    sessions, _ops, _service, _agent_jobs, _clock, submitted, _claim, _run = (
        _issued_job(tmp_path, 340)
    )
    with sessions.begin() as session:
        job = session.get(ArtifactJob, submitted.id)
        assert job is not None
        job.state = "cancelling"
        job.cancel_requested_at = None
    with sessions() as session:
        job = session.get(ArtifactJob, submitted.id)
        assert job is not None
        assert ajs.state_of(job) == ajs.OBSERVING
        assert ajs.cancel_requested_at(job) is not None
        assert ajs.is_live(job) and not ajs.is_ended(job)
        for word in ("draft", "ready"):
            job.state, job.preparation = word, None
            assert ajs.state_of(job) is None and ajs.preparation_of(job) == word
        job.state = "waiting-for-operator"
        assert ajs.state_of(job) == ajs.NEEDS_OPERATOR
        session.rollback()


def test_the_startup_adoption_rewrites_old_rows_once(tmp_path) -> None:
    sessions, _ops, _service, _agent_jobs, _clock, submitted, _claim, _run = (
        _issued_job(tmp_path, 341)
    )
    with sessions.begin() as session:
        job = session.get(ArtifactJob, submitted.id)
        assert job is not None
        job.state = "cancelling"
        job.cancel_requested_at = None
    with sessions.begin() as session:
        connection = session.connection()
        assert ajs.adopt_legacy_artifact_jobs(connection) == 1
        assert ajs.adopt_legacy_artifact_jobs(connection) == 0  # idempotent
    with sessions() as session:
        job = session.get(ArtifactJob, submitted.id)
        assert job is not None
        assert job.state == ajs.OBSERVING
        assert job.cancel_requested_at is not None
    with sessions.begin() as session:
        job = session.get(ArtifactJob, submitted.id)
        assert job is not None
        job.state = "draft"
    with sessions.begin() as session:
        assert ajs.adopt_legacy_artifact_jobs(session.connection()) == 1
    with sessions() as session:
        job = session.get(ArtifactJob, submitted.id)
        assert job is not None
        assert job.state is None and job.preparation == "draft"


def test_a_new_job_is_born_preparing_with_no_state() -> None:
    job = ArtifactJobAdapter.new_job(id="j")
    assert job.state is None and job.preparation == ajs.DRAFT
    ArtifactJobAdapter.mark_ready(job, NOW)
    assert job.state is None and job.preparation == ajs.READY
    ArtifactJobAdapter.mark_submitted(job, "op", NOW)
    assert job.state == ajs.QUEUED and job.preparation is None
