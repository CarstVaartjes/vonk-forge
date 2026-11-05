"""The Spark order on the shared lifecycle core.

``test_lifecycle_core`` proves the core as a table.  This module proves the
adapter and the reconcile loop against the stored order: what a stored (and a
legacy) order *is* to the core, the single projection back onto the rows, and the
promises the audit asked for: an agent-reported waiting body is retried, never
executed work is retried, no operator wait without an action, a cancel completes
past dead work, a legacy row adopts and heals, and a restart in the middle of an
operation is harmless.
"""

# ruff: noqa: F811 - tests take the imported ``agent_service`` fixture by name
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentOperation as ProtocolAgentOperation
from vonk_agent_protocol import LifecycleState, ObservationCause, WaitReason
from vonk_control import agent_operation_states as aos
from vonk_control.agent_jobs import (
    AgentJobService,
    operator_resume_candidates_in_session,
)
from vonk_control.lifecycle import (
    LOST_OBSERVATION_BOUND,
    OBSERVE_BUDGET,
    STOP_BUDGET,
    Effect,
    State,
    Tick,
    transition,
)
from vonk_control.lifecycle.agent_operation import (
    AgentOperationAdapter,
    aggregate_parent_state,
    parked_orders,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
)

from .runtime_identity_support import claim_agent
from .test_agent_jobs import (
    COMMIT,
    NODE_A,
    STOP_PAYLOAD,
    _restart_interrupted_result,
    canonical_start_payload,
    distribution_payload,
    job_state,
    parent,
)
from .test_agent_jobs import service as agent_service  # noqa: F401 - the fixture

KIND_STOP = ProtocolAgentOperation.RECIPE_STOP.value
KIND_DISTRIBUTION = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
_VECTORS = Path(__file__).parents[2] / "agent_protocol/src/vonk_agent_protocol/vectors"


def _job_run_payload() -> tuple[str, dict[str, object]]:
    vector = json.loads((_VECTORS / "recipe-job-run-claim-v1.json").read_text())
    return vector["operation"], vector["payload"]


def _stored(sessions, operation_id: str) -> AgentOperation:
    with sessions() as session:
        stored = session.get(AgentOperation, operation_id)
        assert stored is not None
        session.expunge(stored)
        return stored


def _park(sessions, operation_id: str, **fields: object) -> None:
    """Write the row an order leaves behind when it waits for an operator."""

    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation_id)
        assert stored is not None
        stored.state = LifecycleState.NEEDS_OPERATOR.value
        stored.next_action_at = None
        for name, value in fields.items():
            setattr(stored, name, value)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation_id,
                AgentOperationAttempt.attempt == stored.current_attempt,
            )
        )
        if attempt is not None and attempt.state == "running":
            attempt.state = LifecycleState.OBSERVING.value
            attempt.observation_cause = ObservationCause.LEASE_LAPSED.value


def _flag_cancel(sessions, parent_job_id: str, now: datetime) -> None:
    with sessions.begin() as session:
        job = session.get(Job, parent_job_id)
        assert job is not None
        job.result = {"cancel_requested": True, "cancel_requested_at": now.isoformat()}


def _bound_adapter(sessions, clock):
    return AgentOperationAdapter(
        sessions=sessions,
        clock=clock,
        resume_candidates=operator_resume_candidates_in_session,
    )


# ------------------------------------------------------------------- adopt


def test_a_stored_order_maps_onto_the_core_states(agent_service) -> None:
    jobs, sessions, clock = agent_service
    adapter = _bound_adapter(sessions, clock)
    queued = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    assert adapter.adopt(_stored(sessions, queued.id)).state is State.QUEUED

    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    running = adapter.adopt(_stored(sessions, queued.id))
    assert running.state is State.RUNNING and running.attempt == 1
    assert running.effect is Effect.ISSUED and running.lease_deadline is not None

    # A waiting order is a wait; a backing-off one with a schedule is a retry.
    _park(sessions, queued.id)
    parked = adapter.adopt(_stored(sessions, queued.id))
    assert parked.state is State.NEEDS_OPERATOR and parked.next_action_at is None
    with sessions.begin() as session:
        stored = session.get(AgentOperation, queued.id)
        stored.state, stored.next_action_at = LifecycleState.BACKOFF.value, clock.now
    scheduled = adapter.adopt(_stored(sessions, queued.id))
    assert scheduled.state is State.BACKOFF and scheduled.next_action_at == clock.now


# ------------------------------------------------------------ the kind's facts


@pytest.mark.parametrize(
    "kind",
    [ProtocolAgentOperation.RECIPE_JOB_RUN, ProtocolAgentOperation.AGENT_UPGRADE],
)
def test_unknown_effect_is_lost_or_retried_without_blindly_repeating_user_jobs(
    kind,
) -> None:
    """Catches repeating or failing an unobserved user effect, and parking rebuildable work."""
    from sqlalchemy.orm import Session
    from vonk_control.lifecycle.types import Lifecycle, Observed

    now = datetime(2026, 10, 6, tzinfo=UTC)
    with Session() as session:
        adapter = AgentOperationAdapter(session=session, clock=lambda: now)
        original = Lifecycle(
            id="unknown-effect",
            kind=kind.value,
            state=State.OBSERVING,
            attempt=1,
            effect=Effect.UNKNOWN,
            observe_count=OBSERVE_BUDGET,
            next_action_at=now,
        )
        after = transition(original, Observed(Effect.UNKNOWN), adapter, now).row
        assert after.attempt == original.attempt
        if kind is ProtocolAgentOperation.RECIPE_JOB_RUN:
            # Lost, never failed: the job may still run, so it is observed again.
            assert after.state is State.OBSERVING
            assert after.next_action_at is not None and after.next_action_at > now
        else:
            assert after.state is State.BACKOFF
            assert after.next_action_at is not None and after.next_action_at > now


def _exit_receipt(code: int) -> dict[str, object]:
    """The stored document of a job's measured exit (its ``RecipeJobRunResult``)."""

    from vonk_agent_protocol import (
        RecipeJobFile,
        RecipeJobRunResult,
        canonical_message,
        recipe_job_manifest_document,
        recipe_job_manifest_sha256,
    )

    _kind, payload = _job_run_payload()
    empty: tuple[RecipeJobFile, ...] = ()
    receipt = RecipeJobRunResult.model_validate(
        {
            "job_id": payload["job_id"],
            "run_id": payload["run_id"],
            "exit_code": code,
            "output_manifest": {
                **recipe_job_manifest_document(empty),
                "manifest_sha256": recipe_job_manifest_sha256(empty),
            },
            "evidence": {"elapsed_milliseconds": 5, "peak_memory_bytes": None},
        }
    )
    return json.loads(canonical_message(receipt))


def test_spark_order_observe_reads_its_exact_attempt(agent_service) -> None:
    """Class 2 guard: a Spark order observes the receipt of its own attempt.

    Wrong implementations caught: a constant ``unknown`` observation (a user's
    job was failed by its budget although its exit was receipted), and a
    receipt read for an attempt it does not belong to.
    """

    jobs, sessions, clock = agent_service
    kind, payload = _job_run_payload()
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    clock.advance(seconds=45)
    jobs.reconcile_orders()  # the lease lapsed: the job's effect is unknown
    adapter = _bound_adapter(sessions, clock)
    row = adapter.adopt(_stored(sessions, operation.id))
    assert row.state is State.OBSERVING
    assert adapter.observe(row).effect is Effect.UNKNOWN

    # A late exit receipt is stored on the lapsed attempt (as the late-result
    # ingress keeps it): that attempt's process is proven to have ended.
    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation.id)
        attempt = adapter.attempt_of(session, stored)
        assert attempt is not None
        attempt.result = _exit_receipt(3)
    assert adapter.observe(row).effect is Effect.STOPPED
    # Identity: the receipt is attempt 1's, never another attempt's.
    assert adapter.observe(replace(row, attempt=2)).effect is Effect.UNKNOWN

    # The observed exit ends the order at its next look; it never runs again.
    due = _stored(sessions, operation.id).next_action_at
    assert due is not None
    clock.now = due.replace(tzinfo=UTC) + timedelta(seconds=1)
    jobs.reconcile_orders()
    assert _stored(sessions, operation.id).state not in aos.LIVE
    assert claim_agent(jobs, NODE_A, "serial-a") is None


def _reconcile_to_next_look(jobs, sessions, clock, operation_id: str) -> AgentOperation:
    due = _stored(sessions, operation_id).next_action_at
    assert due is not None
    clock.now = max(clock.now, due.replace(tzinfo=UTC)) + timedelta(seconds=1)
    jobs.reconcile_orders()
    return _stored(sessions, operation_id)


def test_a_lost_job_order_ends_failed_after_the_bound_and_a_fresh_request_runs(
    agent_service,
) -> None:
    """Wrong implementation (main before this fix): the lost order kept its
    node's lane for ever, so a fresh request for the same Spark never ran.
    One look short of the bound it still waits; the last look ends it failed
    with typed evidence and frees the lane."""

    jobs, sessions, clock = agent_service
    kind, payload = _job_run_payload()
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    clock.advance(seconds=45)
    jobs.reconcile_orders()
    fresh = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)

    stored = _stored(sessions, operation.id)
    for _ in range(OBSERVE_BUDGET + 2 * LOST_OBSERVATION_BOUND):
        if stored.observe_count >= OBSERVE_BUDGET + LOST_OBSERVATION_BOUND - 1:
            break
        stored = _reconcile_to_next_look(jobs, sessions, clock, operation.id)
    # One unknown look short of the bound: still lost, still holding its lane.
    assert stored.state == aos.OBSERVING
    assert stored.observe_count == OBSERVE_BUDGET + LOST_OBSERVATION_BOUND - 1

    ended = _reconcile_to_next_look(jobs, sessions, clock, operation.id)
    assert ended.state == aos.FAILED
    assert ended.status_reason is not None
    assert ended.status_reason.startswith(WaitReason.RUNTIME_EFFECT_UNCONFIRMED.value)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    assert _stored(sessions, fresh.id).current_attempt == 1


def test_a_lost_job_order_is_observed_at_its_own_cadence_never_ended(
    agent_service,
) -> None:
    """Wrong implementations caught: the spent budget or the passed recovery
    deadline failed a user's job that may still run, or the lost order became
    due on every pass and crowded out the orders behind it."""

    jobs, sessions, clock = agent_service
    kind, payload = _job_run_payload()
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    clock.advance(seconds=45)
    jobs.reconcile_orders()
    for _ in range(OBSERVE_BUDGET + 1):
        due = _stored(sessions, operation.id).next_action_at
        assert due is not None
        clock.now = max(clock.now, due.replace(tzinfo=UTC)) + timedelta(seconds=1)
        jobs.reconcile_orders()
    clock.advance(seconds=7200)
    jobs.reconcile_orders()

    lost = _stored(sessions, operation.id)
    assert lost.state == aos.OBSERVING
    assert (
        OBSERVE_BUDGET <= lost.observe_count < OBSERVE_BUDGET + LOST_OBSERVATION_BOUND
    )
    assert lost.recovery_deadline is not None
    assert lost.recovery_deadline.replace(tzinfo=UTC) < clock.now
    assert lost.next_action_at is not None
    # Its next look is at the bounded backoff's ceiling: later than a short
    # wait re-check, since each look rewrites the row under its node locks.
    from vonk_control.lifecycle.core import RECOVERY, WAIT_STEP

    wait = RECOVERY.next_attempt(
        operation.id, WAIT_STEP, clock.now, ongoing_intent=True
    )
    assert wait is not None and lost.next_action_at.replace(tzinfo=UTC) > wait
    assert lost.next_action_at.replace(tzinfo=UTC) <= clock.now + 2 * timedelta(
        seconds=RECOVERY.max_delay_seconds
    )
    with sessions() as session:
        due_now = session.scalars(
            select(AgentOperation.id).where(parked_orders(clock.now))
        ).all()
    assert operation.id not in due_now  # due at its next look, not every pass
    assert claim_agent(jobs, NODE_A, "serial-a") is None


def test_actions_are_advertised_exactly_when_the_job_endpoints_accept_them(
    agent_service,
) -> None:
    jobs, sessions, clock = agent_service
    kind, payload = _job_run_payload()
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    clock.advance(seconds=45)
    _park(sessions, operation.id)
    adapter = _bound_adapter(sessions, clock)
    row = adapter.adopt(_stored(sessions, operation.id))
    assert adapter.actions(row) == ("resume", "retire")
    _flag_cancel(sessions, operation.parent_job_id, clock.now)
    assert adapter.actions(row) == ()  # the endpoints refuse a cancelled job's resume


# -------------------------------------------------------- rule 1: always retried


def test_a_never_executed_order_parked_for_an_operator_is_queued_again(
    agent_service,
) -> None:
    """A wait written for work that never ran must not outlive its cause."""

    jobs, sessions, clock = agent_service
    kind, payload = _job_run_payload()  # even an irreversible kind: it never ran
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    _park(sessions, operation.id)  # attempt 0
    assert _stored(sessions, operation.id).current_attempt == 0

    assert jobs.reconcile_orders() is True

    stored = _stored(sessions, operation.id)
    assert stored.state == "queued" and stored.next_action_at is not None
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None and claim.operation.value == kind


def test_a_never_executed_start_parked_for_an_operator_is_queued_again(
    agent_service,
) -> None:
    jobs, sessions, clock = agent_service
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.RECIPE_START.value,
        COMMIT,
        canonical_start_payload(start_deadline=clock.now + timedelta(minutes=30)),
    )
    _park(sessions, operation.id)  # a start that never ran

    assert jobs.reconcile_orders() is True

    assert _stored(sessions, operation.id).state == "queued"
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None and claim.operation.value == "recipe.start"


def test_a_restart_safe_order_parked_for_an_operator_is_retried(agent_service) -> None:
    jobs, sessions, clock = agent_service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=45)
    jobs.record_late_result(_restart_interrupted_result(claim, KIND_STOP))
    _park(sessions, operation.id)  # the shape before the fix
    before = _stored(sessions, operation.id)
    assert before.next_action_at is None

    assert jobs.reconcile_orders() is True

    after = _stored(sessions, operation.id)
    assert after.state == aos.BACKOFF and after.next_action_at is not None
    assert job_state(sessions, operation.parent_job_id).state == "queued"
    clock.now = after.next_action_at.replace(tzinfo=UTC) + timedelta(seconds=1)
    retried = claim_agent(jobs, NODE_A, "serial-a")
    assert retried is not None


# ------------------------------------------------ rule 3: no wait without an action


def _after_the_observation_budget(adapter, sessions, clock, operation_id: str):
    """Decide a parked order that has been observed as often as the core allows."""

    with sessions() as session:
        stored = session.get(AgentOperation, operation_id)
        row = adapter.lifecycle(
            stored,
            adapter.attempt_of(session, stored),
            session.get(Job, stored.parent_job_id),
            clock.now,
        )
    observing = replace(
        row,
        state=State.OBSERVING,
        observe_count=OBSERVE_BUDGET,
        next_action_at=clock.now,
    )
    return transition(observing, Tick(), adapter, clock.now + timedelta(hours=1))


def test_irreversible_observation_exhaustion_is_lost_without_an_operator(
    agent_service,
) -> None:
    jobs, sessions, clock = agent_service
    kind, payload = _job_run_payload()
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    clock.advance(seconds=45)
    _park(sessions, operation.id)
    adapter = _bound_adapter(sessions, clock)

    # Advertised actions cannot turn an exhausted unknown into a queue head, and
    # the spent budget does not end a job that may still run: it is observed.
    with_action = _after_the_observation_budget(adapter, sessions, clock, operation.id)
    assert with_action.row.state is State.OBSERVING
    assert with_action.commands != ()
    assert with_action.row.next_action_at is not None
    assert with_action.row.lease_deadline is None

    # the job was cancelled: the endpoints refuse, so there is no action, and the
    # core does not wait for one (it ends the cancel instead)
    _flag_cancel(sessions, operation.parent_job_id, clock.now)
    without_action = _after_the_observation_budget(
        adapter, sessions, clock, operation.id
    )
    assert without_action.row.state is not State.NEEDS_OPERATOR
    for _ in range(STOP_BUDGET + 2):
        clock.advance(seconds=120)
        jobs.reconcile_orders()
        if _stored(sessions, operation.id).state == State.CANCELLED.value:
            break
    assert _stored(sessions, operation.id).state == State.CANCELLED.value
    fresh = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    assert fresh.id != operation.id
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    assert _stored(sessions, fresh.id).current_attempt == 1


# ----------------------------------------------------- rule 4: a cancel completes


def test_a_cancel_completes_past_dead_work(agent_service) -> None:
    """A dead attempt of a cancelled job ends; nothing waits for a receipt."""

    jobs, sessions, clock = agent_service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    _flag_cancel(sessions, parent_job.id, clock.now)
    clock.advance(seconds=45)  # the lease lapsed and the node never comes back

    assert jobs.reconcile_orders() is True

    stored = _stored(sessions, operation.id)
    assert stored.state == "cancelled" and stored.next_action_at is None
    assert job_state(sessions, parent_job.id).state == "cancelled"


def test_a_cancelled_irreversible_order_ends_with_its_effect_unknown(
    agent_service,
) -> None:
    """No Controller-side stop exists for a user's job: the cancel ends after the
    stop budget, never as an operator wait, and never claims the job stopped."""

    jobs, sessions, clock = agent_service
    kind, payload = _job_run_payload()
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, kind, COMMIT, payload)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    _flag_cancel(sessions, parent_job.id, clock.now)
    clock.advance(seconds=45)

    seen: list[str] = []
    for _ in range(STOP_BUDGET + 3):
        jobs.reconcile_orders()
        stored = _stored(sessions, operation.id)
        seen.append(stored.state)
        if stored.state == "cancelled":
            break
        clock.advance(seconds=120)
    assert seen[-1] == "cancelled", seen


def test_an_order_whose_job_ended_ends_with_it(agent_service) -> None:
    jobs, sessions, clock = agent_service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=45)
    jobs.record_late_result(_restart_interrupted_result(claim, KIND_STOP))
    _park(sessions, operation.id)
    with sessions.begin() as session:
        session.get(Job, parent_job.id).state = "failed"

    jobs.reconcile_orders()

    assert _stored(sessions, operation.id).state == "cancelled"


# ------------------------------------------------- legacy rows adopt and heal


def test_a_lapsed_running_order_is_decided_without_the_node_polling(
    agent_service,
) -> None:
    """Before: only another poll of that node decided it, so a dead Spark kept it
    ``running`` forever."""

    jobs, sessions, clock = agent_service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    assert jobs.reconcile_orders() is False  # a live lease is left alone
    assert _stored(sessions, operation.id).state == "running"
    clock.advance(seconds=45)

    assert jobs.reconcile_orders() is True

    stored = _stored(sessions, operation.id)
    assert stored.state == aos.BACKOFF and stored.next_action_at is not None


def test_many_legacy_parked_orders_heal_in_one_pass(agent_service) -> None:
    jobs, sessions, clock = agent_service
    ids = []
    for _ in range(3):
        operation = jobs.enqueue(
            parent(sessions, clock).id,
            NODE_A,
            KIND_DISTRIBUTION,
            COMMIT,
            distribution_payload(COMMIT),
        )
        ids.append(operation.id)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    clock.advance(seconds=45)
    for operation_id in ids:
        _park(sessions, operation_id)
    jobs.reconcile_orders()
    for operation_id in ids:
        stored = _stored(sessions, operation_id)
        # every one is either queued again (it never ran) or scheduled
        assert stored.state == "queued" or stored.next_action_at is not None, (
            stored.state
        )


# ------------------------------------------------- a restart is harmless


def test_a_restart_in_the_middle_of_an_operation_is_harmless(agent_service) -> None:
    jobs, sessions, clock = agent_service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    live = _stored(sessions, operation.id)

    # The Controller restarts: a fresh service finds a live attempt untouched.
    restarted = AgentJobService(sessions, clock=clock)
    assert restarted.reconcile_orders() is False
    assert _stored(sessions, operation.id).state == "running"
    assert _stored(sessions, operation.id).current_attempt == live.current_attempt

    # The lease lapses; it restarts again between and during passes.
    clock.advance(seconds=45)
    first = AgentJobService(sessions, clock=clock)
    assert first.reconcile_orders() is True
    scheduled = _stored(sessions, operation.id)
    again = AgentJobService(sessions, clock=clock)
    assert again.reconcile_orders() is False  # repeating a pass changes nothing
    assert _stored(sessions, operation.id).next_action_at == scheduled.next_action_at
    assert _stored(sessions, operation.id).current_attempt == scheduled.current_attempt

    # The agent's late receipt of the lapsed attempt is still accepted afterwards.
    assert (
        jobs.record_late_result(_restart_interrupted_result(claim, KIND_STOP)) is True
    )


def test_a_pass_that_lost_a_race_leaves_the_row_to_the_next_pass(agent_service) -> None:
    """``save`` applies a decision only to the order it was made from."""

    jobs, sessions, clock = agent_service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=45)
    jobs.record_late_result(_restart_interrupted_result(claim, KIND_STOP))
    _park(sessions, operation.id)

    reconciler = jobs._order_reconciler()
    store = reconciler._store
    (row,) = store.due(clock.now, 10)
    decision = _decision_for(jobs, sessions, clock, operation.id)
    # An operator resumes the order between the read and the write.
    with sessions.begin() as session:
        resumed = session.get(AgentOperation, operation.id)
        resumed.state, resumed.next_action_at = LifecycleState.BACKOFF.value, clock.now
    assert store.save(row, decision.row) is False
    assert _stored(sessions, operation.id).next_action_at is not None


def _decision_for(jobs, sessions, clock, operation_id):
    adapter = _bound_adapter(sessions, clock)
    row = adapter.adopt(_stored(sessions, operation_id))
    return transition(row, Tick(), adapter, clock.now)


# ------------------------------------------------------------ the aggregate


@pytest.mark.parametrize(
    ("children", "cancelled", "expected"),
    [
        ([("running", False)], False, None),
        ([("succeeded", False), ("queued", False)], False, None),
        # every unfinished order is an automatic retry: the job is progressing
        ([("backoff", True), ("succeeded", False)], False, "queued"),
        ([("backoff", True)], True, None),
        (
            [("needs-operator", False), ("succeeded", False)],
            False,
            "needs-operator",
        ),
        ([("failed", False), ("needs-operator", False)], False, "failed"),
        ([("failed", False), ("observing", False)], False, "failed"),
        ([("cancelled", False), ("succeeded", False)], False, "cancelled"),
        ([("succeeded", False), ("succeeded", False)], False, "succeeded"),
        ([], False, None),
    ],
)
def test_the_parent_aggregate(children, cancelled, expected) -> None:
    assert aggregate_parent_state(children, cancel_requested=cancelled) == expected


def test_the_dead_entry_points_are_gone() -> None:
    assert not hasattr(AgentJobService, "wait_for_operator")
    assert not hasattr(AgentJobService, "uncertain")


def test_nodes_never_polled_are_still_reconciled(agent_service) -> None:
    """The sweep needs no contact from the node: it reads the rows."""

    jobs, sessions, clock = agent_service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    with sessions.begin() as session:  # the Spark was revoked meanwhile
        node = session.get(AgentNode, NODE_A)
        node.state = "revoked"
        node.revoked_at = clock.now
    clock.advance(seconds=45)
    from types import SimpleNamespace

    from .non_blocking import assert_ended_without_blocking

    def end(_):
        assert jobs.reconcile_orders()
        return _stored(sessions, operation.id)

    def fresh(_):
        with sessions.begin() as session:
            node = session.get(AgentNode, NODE_A)
            node.state = "active"
            node.revoked_at = None
        successor = jobs.enqueue(
            parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
        )
        assert claim_agent(jobs, NODE_A, "serial-a") is not None
        return _stored(sessions, successor.id)

    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        operation,
        end=end,
        fresh=fresh,
        request_key=lambda row: row.id,
    )


# ------------------------------------ the stored vocabulary is the core's


def test_a_lapsed_attempt_is_observed_with_its_cause_and_an_old_one_reads_the_same(
    agent_service,
) -> None:
    jobs, sessions, clock = agent_service
    order = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    clock.advance(seconds=45)
    assert jobs.reconcile_orders() is True

    stored = _stored(sessions, order.id)
    assert stored.state == aos.BACKOFF
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == order.id
            )
        )
        assert attempt.state == aos.OBSERVING
        assert attempt.observation_cause == "lease-lapsed"
        assert aos.attempt_lapsed(attempt) and not aos.attempt_reported_unknown(attempt)
        found = session.scalars(
            select(AgentOperationAttempt.id).where(
                aos.sql_attempt_lapsed(AgentOperationAttempt)
            )
        ).all()
        assert attempt.id in found

        session.rollback()


def test_a_parked_order_is_found_by_every_selection(
    agent_service,
) -> None:
    jobs, sessions, clock = agent_service
    order = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    for word in (
        aos.NEEDS_OPERATOR,
        aos.BACKOFF,
        aos.OBSERVING,
    ):
        with sessions.begin() as session:
            session.get(AgentOperation, order.id).state = word
        with sessions() as session:
            for words in (aos.PARKED, aos.LIVE, aos.RUNNING_OR_PARKED):
                assert (
                    order.id
                    in session.scalars(
                        select(AgentOperation.id).where(AgentOperation.state.in_(words))
                    ).all()
                ), (word, words)
        assert aos.order_is_parked(word)
    assert not aos.order_is_parked("running") and not aos.order_is_parked("queued")


def test_the_agent_word_for_unknown_is_mapped_at_ingress_to_an_observed_attempt(
    agent_service,
) -> None:
    attempt = AgentOperationAttempt()
    aos.record_wire_state(attempt, aos.WIRE_UNKNOWN)
    assert attempt.state == aos.OBSERVING
    assert aos.attempt_reported_unknown(attempt)
    assert aos.attempt_wire_state(attempt) == aos.WIRE_UNKNOWN
    aos.record_wire_state(attempt, "failed")
    assert attempt.state == "failed" and attempt.observation_cause is None
    assert aos.attempt_wire_state(attempt) == "failed"
    aos.lapse(attempt)
    assert aos.attempt_lapsed(attempt)


@pytest.mark.parametrize(
    "kind",
    [ProtocolAgentOperation.RECIPE_BUILD, ProtocolAgentOperation.RECIPE_BUILD_CLEANUP],
)
def test_unknown_build_effect_retries_after_the_observation_budget(kind) -> None:
    """Rebuildable work must not inherit the irreversible user-job observation loop."""
    from sqlalchemy.orm import Session
    from vonk_control.lifecycle.types import Claimed, Lifecycle, Observed

    now = datetime(2026, 10, 6, tzinfo=UTC)
    with Session() as session:
        adapter = AgentOperationAdapter(session=session, clock=lambda: now)
        row = Lifecycle(
            id="build",
            kind=kind.value,
            state=State.OBSERVING,
            attempt=1,
            effect=Effect.UNKNOWN,
            observe_count=OBSERVE_BUDGET,
            next_action_at=now,
        )
        scheduled = transition(row, Observed(Effect.UNKNOWN), adapter, now).row
        assert scheduled.state is State.BACKOFF
        assert scheduled.next_action_at is not None and scheduled.next_action_at > now
        due = scheduled.next_action_at
        claimed = transition(
            scheduled,
            Claimed(2, "fresh-fence", due + timedelta(seconds=30)),
            adapter,
            due,
        ).row
        assert claimed.state is State.RUNNING
        assert claimed.fence == "fresh-fence"
