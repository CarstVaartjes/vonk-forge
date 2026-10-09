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
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    LifecycleState,
    OutcomeDone,
    OutcomeKind,
    RecipeStopResult,
)
from vonk_control import artifact_job_states as ajs
from vonk_control.agent_jobs import AgentJobService
from vonk_control.artifact_jobs import (
    ArtifactJobResponse,
    ArtifactJobView,
)
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.lifecycle import Effect, Lifecycle, State
from vonk_control.lifecycle.artifact_job import ArtifactJobAdapter
from vonk_control.models import (
    AgentOperation,
    AgentOperationAttempt,
    ArtifactJob,
    Job,
    RecipeRun,
)
from vonk_control.recipe_operations import (
    RecipeArtifactJobCancellationPending,
    RecipeOperationView,
)

from .agent_fences import fenced_operation
from .non_blocking import assert_ended_without_blocking, assert_no_orphaned_holds
from .runtime_identity_support import claim_agent
from .test_artifact_jobs import (
    NOW,
    MutableClock,
    cancellation_result,
    create_artifact_job,
    running_artifact_service,
    submitted_artifact_job,
)
from .test_recipe_operations import _issue_exact_stop_grant

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

    draft = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000311"),
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


def test_a_lapsed_job_ends_without_reissue_and_admits_a_fresh_job(tmp_path) -> None:
    """Catches unknown effects turning into an endless owner or a false stop."""
    sessions, operations, service, agent_jobs, clock, submitted, claim, run_id = (
        _issued_job(tmp_path, 320)
    )
    operations._clock = clock
    service._clock = clock
    clock.advance(seconds=31)
    agent_jobs.reconcile_orders()
    for _ in range(60):
        if service.get(submitted.id).state in ajs.ENDED:
            break
        clock.advance(seconds=120)
        agent_jobs.reconcile_orders()
    assert service.get(submitted.id).state in ajs.ENDED
    assert service.get(submitted.id).state != LifecycleState.SUCCEEDED
    ended = service.get(submitted.id)
    assert ended.supported_actions == ()
    assert ended.result_evidence is not None
    assert ended.result_evidence.active_scope_may_remain is True
    with sessions() as session:
        operation = fenced_operation(sessions, claim)
        assert operation.current_attempt == 1  # Never repeats the user's job.
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == LifecycleState.RUNNING
    restarted = AgentJobService(sessions, clock=clock)
    operations._agent_jobs = restarted
    fresh = submitted_artifact_job(service, run_id, request_suffix=321)
    assert fresh.operation_id != submitted.operation_id
    assert fresh.state == LifecycleState.QUEUED


def test_a_lapsed_job_is_stoppable_without_operator_wait_and_allows_fresh_work(
    tmp_path,
) -> None:
    sessions, operations, service, agent_jobs, clock, submitted, _claim, run_id = (
        _issued_job(tmp_path, 320)
    )
    clock.advance(seconds=31)
    agent_jobs.reconcile_orders()
    assert service.get(submitted.id).state == ajs.OBSERVING
    service.cancel(
        submitted.id, actor="operator", request_id=CANCEL_KEY, reason="lost job"
    )
    assert (
        _drive(service, agent_jobs, clock, submitted.id, until=ajs.CANCELLED)
        == ajs.CANCELLED
    )
    ended = service.get(submitted.id)
    assert ended.supported_actions == ()
    assert ended.result_evidence is not None
    assert ended.result_evidence.active_scope_may_remain is True
    # Ending an uncertain one-shot job must not block the fresh exact Stop.
    plan = operations.preview_stop(run_id)
    assert plan.allowed

    def request_key(view: ArtifactJobView | RecipeOperationView) -> str:
        if isinstance(view, ArtifactJobView):
            assert view.submit_request_id is not None
            return view.submit_request_id
        with sessions() as session:
            parent = session.get(Job, view.id)
            assert parent is not None
            return parent.request_id

    def assert_released() -> None:
        with sessions() as session:
            assert_no_orphaned_holds(session)

    def end_job(
        _view: ArtifactJobView | RecipeOperationView,
    ) -> ArtifactJobView | RecipeOperationView:
        return service.get(submitted.id)

    _ended, fresh = assert_ended_without_blocking(
        sessions,
        submitted,
        end=end_job,
        fresh=lambda _: operations.stop(
            run_id,
            plan_digest=plan.plan_digest,
            actor="operator",
            request_id=OTHER_KEY,
        ),
        request_key=request_key,
        assert_released=assert_released,
    )
    assert fresh.id != submitted.operation_id


def test_a_live_job_does_not_block_fresh_submission_or_share_its_execution_slot(
    tmp_path,
):
    """Admission is queued; a healthy first executor retains the physical slot."""
    sessions, operations, service, jobs, clock, original, claim, run_id = _issued_job(
        tmp_path, 322
    )
    operations._clock = clock
    service._clock = clock
    fresh = submitted_artifact_job(service, run_id, request_suffix=323)
    assert fresh.operation_id != original.operation_id
    assert fresh.state == LifecycleState.QUEUED
    assert (
        claim_agent(jobs, fenced_operation(sessions, claim).node_id, "serial-0") is None
    )
    assert service.get(original.id).state == LifecycleState.RUNNING
    assert fenced_operation(sessions, claim).current_attempt == 1


def test_lost_irreversible_job_exact_stop_receipt_allows_fresh_run_and_claim(tmp_path):
    """Lease loss cannot release claims; the exact late Stop acknowledgement can."""
    sessions, operations, service, agent_jobs, clock, submitted, claim, run_id = (
        _issued_job(tmp_path, 900)
    )
    operations._clock = clock
    service._clock = clock
    clock.advance(seconds=31)
    agent_jobs.reconcile_orders()
    assert (
        _drive(service, agent_jobs, clock, submitted.id, until=ajs.OBSERVING)
        == ajs.OBSERVING
    )
    waiting = service.get(submitted.id)
    assert waiting.supported_actions == ("stop",)

    # A restarted Controller retains the same authoritative rows and receipt fence.
    restarted = AgentJobService(sessions, clock=clock)

    def consume(session, operation, attempt, message):
        service.consume_agent_result(session, operation, attempt, message)
        operations.consume_agent_result(session, operation, attempt, message)

    restarted.set_result_consumer(consume)
    operations._agent_jobs = restarted
    restarted.reconcile_orders()
    assert service.get(submitted.id).state == ajs.OBSERVING
    plan = operations.preview_stop(run_id)
    assert plan.allowed
    stop_key = "00000000-0000-4000-8000-000000000905"
    with pytest.raises(RecipeArtifactJobCancellationPending):
        operations.stop(
            run_id, plan_digest=plan.plan_digest, actor="operator", request_id=stop_key
        )
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "running"
        installation_id = run.installation_id
        order = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == submitted.operation_id
            )
        )
        assert order is not None
        node_id = order.node_id
        old_order_id = order.id

    acknowledged = cancellation_result(
        claim, submitted, state="cancelled", reason="controller cancellation requested"
    )
    assert restarted.record_late_result(acknowledged)
    assert service.get(submitted.id).state == "cancelled"
    stopped = operations.stop(
        run_id, plan_digest=plan.plan_digest, actor="operator", request_id=stop_key
    )
    assert stopped.state == "running"
    exact, stop_payload, _grant = _issue_exact_stop_grant(
        sessions,
        node_id=node_id,
        certificate_serial="serial-0",
        grant_now=clock(),
    )
    assert exact.fence != claim.fence
    assert stop_payload.run_id == run_id
    assert stop_payload.target_runtime_id == submitted.id
    native = fenced_operation(sessions, exact)
    assert native.parent_job_id == stopped.id and native.id != old_order_id
    restarted.record_result(
        AgentResult(
            fence=exact.fence,
            state=AgentResultState.SUCCEEDED,
            result=OutcomeDone(kind=OutcomeKind.DONE, result=RecipeStopResult()),
        )
    )
    stopped = operations.get(stopped.id)
    assert stopped.state == "succeeded"

    # Recovery includes a fresh real observation, never fabricated capacity release.
    InventoryRepository(sessions, clock=clock).record(
        InventorySnapshotInput(
            node_id,
            clock(),
            10_000,
            8_000,
            10_000,
            8_000,
            10_000,
            8_000,
            1,
            False,
            ("runtime.vonk.v1", "recipe.image.pull.v1", "recipe.operations.v1"),
            memory_pool="shared",
        )
    )
    fresh_plan = operations.preview_run(installation_id, "image-job")
    assert fresh_plan.allowed

    def assert_released():
        with sessions() as session:
            old_run = session.get(RecipeRun, run_id)
            assert old_run is not None and old_run.state == "stopped"
            assert_no_orphaned_holds(session)

    def stored_request_key(view: RecipeOperationView) -> str:
        with sessions() as session:
            parent = session.get(Job, view.id)
            assert parent is not None
            return parent.request_id

    replay, fresh_run = assert_ended_without_blocking(
        fresh_plan,
        stopped,
        end=lambda _: operations.stop(
            run_id, plan_digest=plan.plan_digest, actor="operator", request_id=stop_key
        ),
        fresh=lambda admitted: operations.activate_job_run(
            admitted,
            plan_digest=admitted.plan_digest,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000906",
        ),
        assert_released=assert_released,
        request_key=stored_request_key,
    )
    assert replay == stopped
    assert fresh_run.owner_id != run_id
    fresh = submitted_artifact_job(service, fresh_run.owner_id, request_suffix=908)
    fresh_claim = claim_agent(restarted, node_id, "serial-0")
    assert fresh_claim is not None
    assert fresh_claim.fence != claim.fence
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == fresh.operation_id
            )
        )
        assert operation is not None and operation.parent_job_id == fresh.operation_id
        assert operation.id != old_order_id and operation.state == "running"


# ------------------------------------------------------- legacy adoption


@pytest.mark.usefixtures("damaged_json_rows")
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


def test_typed_lease_expiry_does_not_claim_absence_without_exact_stop_receipt(
    tmp_path,
) -> None:
    """A stored typed expiry cause is not an observation that the target stopped."""

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
    assert stopped.state == "running"
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


@pytest.mark.postgres
def test_busy_job_projection_does_not_hold_up_other_jobs_or_fresh_admission(
    tmp_path, postgres_engine
) -> None:
    """A blocking FOR UPDATE would stall this pass at the first occupied job."""
    from threading import Event, Thread

    sessions, _, _, service, run_id, _ = running_artifact_service(
        tmp_path, engine=postgres_engine
    )
    first = submitted_artifact_job(service, run_id, request_suffix=901)
    second = submitted_artifact_job(service, run_id, request_suffix=911)
    with sessions.begin() as session:
        for view in (first, second):
            job = session.get(ArtifactJob, view.id)
            assert job is not None
            operation = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == job.operation_id
                )
            )
            assert operation is not None
            operation.state = LifecycleState.CANCELLED
    completed = Event()
    with sessions.begin() as occupied:
        occupied.scalar(
            select(ArtifactJob).where(ArtifactJob.id == first.id).with_for_update()
        )

        def observe() -> None:
            ArtifactJobAdapter(sessions=sessions, clock=lambda: NOW).reconcile()
            completed.set()

        observer = Thread(target=observe)
        observer.start()
        try:
            assert completed.wait(timeout=2)
            assert service.get(second.id).state == LifecycleState.CANCELLED
            fresh = submitted_artifact_job(service, run_id, request_suffix=921)
            assert fresh.id not in {first.id, second.id}
            assert fresh.state == LifecycleState.QUEUED
        finally:
            # Release the actual PostgreSQL lock before joining a failed worker.
            occupied.rollback()
            observer.join(timeout=5)
    assert not observer.is_alive()
    ArtifactJobAdapter(sessions=sessions, clock=lambda: NOW).reconcile()
    assert service.get(first.id).state == LifecycleState.CANCELLED


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_terminal_projection_cannot_veto_current_fenced_stop_receipt(tmp_path):
    """A damaged old metric record cannot block a fresh explicit user job."""
    sessions, _, service, agent_jobs, _, submitted, claim, run_id = _issued_job(
        tmp_path, 990
    )
    service.cancel(
        submitted.id, actor="operator", request_id=CANCEL_KEY, reason="cancel"
    )
    agent_jobs.record_result(
        cancellation_result(
            claim,
            submitted,
            state=AgentResultState.FAILED,
            reason="cancelled",
        )
    )
    with sessions.begin() as session:
        prior = session.get(ArtifactJob, submitted.id)
        assert prior is not None
        prior.result_evidence = {"damaged": True}

    def submit_fresh_job():
        return submitted_artifact_job(service, run_id, request_suffix=992)

    fresh = submit_fresh_job()
    assert fresh.id != submitted.id and fresh.operation_id is not None
    with sessions() as session:
        issued = list(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == submitted.operation_id
                )
            )
        )
        assert len(issued) == 1  # the uncertain destructive user job was never replayed
