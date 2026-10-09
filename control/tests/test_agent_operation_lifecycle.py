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
from vonk_control import agent_operation_states as aos
from vonk_control.agent_jobs import (
    AgentJobService,
    operator_resume_candidates_in_session,
)
from vonk_control.lifecycle import (
    OBSERVE_BUDGET,
    STOP_BUDGET,
    Effect,
    State,
    Tick,
    transition,
)
from vonk_control.lifecycle.agent_operation import (
    IRREVERSIBLE_OPERATIONS,
    AgentOperationAdapter,
    adopt_legacy_orders,
    aggregate_parent_state,
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


def _park_like_a_legacy_wait(sessions, operation_id: str, **fields: object) -> None:
    """Write the row an order leaves behind when it waits for an operator."""

    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation_id)
        assert stored is not None
        stored.state = "waiting-for-operator"
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
            attempt.state = "expired"


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

    # A waiting order with a schedule is a retry; without one it is a wait.
    _park_like_a_legacy_wait(sessions, queued.id)
    parked = adapter.adopt(_stored(sessions, queued.id))
    assert parked.state is State.NEEDS_OPERATOR and parked.next_action_at is None
    with sessions.begin() as session:
        session.get(AgentOperation, queued.id).next_action_at = clock.now
    scheduled = adapter.adopt(_stored(sessions, queued.id))
    assert scheduled.state is State.BACKOFF and scheduled.next_action_at == clock.now


def test_a_row_written_before_the_core_adopts_its_retry_schedule(agent_service) -> None:
    jobs, sessions, clock = agent_service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    due = clock.now + timedelta(seconds=30)
    _park_like_a_legacy_wait(
        sessions,
        operation.id,
        retry_disposition="retry",
        retry_disposition_attempt=1,
        retry_due_at=due,
    )
    adapter = _bound_adapter(sessions, clock)
    adopted = adapter.adopt(_stored(sessions, operation.id))
    assert adopted.state is State.BACKOFF and adopted.next_action_at == due
    # A disposition keyed to another attempt never authorises this one.
    _park_like_a_legacy_wait(sessions, operation.id, retry_disposition_attempt=7)
    assert adapter.adopt(_stored(sessions, operation.id)).state is State.NEEDS_OPERATOR


def test_startup_adoption_moves_the_legacy_schedule_onto_next_action_at(
    agent_service,
) -> None:
    jobs, sessions, clock = agent_service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    due = clock.now + timedelta(seconds=30)
    _park_like_a_legacy_wait(
        sessions,
        operation.id,
        retry_disposition="retry",
        retry_disposition_attempt=1,
        retry_due_at=due,
    )
    other = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    with sessions.begin() as session:
        assert adopt_legacy_orders(session.connection()) == 1
    stored = _stored(sessions, operation.id)
    assert stored.next_action_at is not None
    assert stored.next_action_at.replace(tzinfo=UTC) == due
    assert stored.retry_disposition is None and stored.retry_due_at is None
    assert _stored(sessions, other.id).next_action_at is None  # untouched
    with sessions.begin() as session:  # idempotent
        assert adopt_legacy_orders(session.connection()) == 0
    clock.now = due + timedelta(seconds=1)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None  # claimable as it was


# ------------------------------------------------------------ the kind's facts


def test_the_irreversible_kinds_are_exactly_the_ones_a_repeat_could_not_undo() -> None:
    assert IRREVERSIBLE_OPERATIONS == {
        ProtocolAgentOperation.RECIPE_JOB_RUN.value,
    }
    assert ProtocolAgentOperation.AGENT_UPGRADE.value not in IRREVERSIBLE_OPERATIONS


def test_actions_are_advertised_exactly_when_the_job_endpoints_accept_them(
    agent_service,
) -> None:
    jobs, sessions, clock = agent_service
    kind, payload = _job_run_payload()
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    clock.advance(seconds=45)
    _park_like_a_legacy_wait(sessions, operation.id)
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
    _park_like_a_legacy_wait(sessions, operation.id)  # attempt 0
    assert _stored(sessions, operation.id).current_attempt == 0

    assert jobs.reconcile_orders() is True

    stored = _stored(sessions, operation.id)
    assert stored.state == "queued" and stored.next_action_at is None
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
    _park_like_a_legacy_wait(sessions, operation.id)  # a start that never ran

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
    _park_like_a_legacy_wait(sessions, operation.id)  # the shape before the fix
    before = _stored(sessions, operation.id)
    assert before.next_action_at is None

    assert jobs.reconcile_orders() is True

    after = _stored(sessions, operation.id)
    assert after.state == aos.BACKOFF and after.next_action_at is not None
    assert "retry scheduled at" in (after.status_reason or "")
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


def test_an_irreversible_unknown_ends_without_operator_parking(
    agent_service,
) -> None:
    jobs, sessions, clock = agent_service
    kind, payload = _job_run_payload()
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    clock.advance(seconds=45)
    _park_like_a_legacy_wait(sessions, operation.id)
    adapter = _bound_adapter(sessions, clock)

    # Advertised actions do not turn uncertainty into an operator wait.
    with_action = _after_the_observation_budget(adapter, sessions, clock, operation.id)
    assert with_action.row.state is State.FAILED
    assert with_action.row.next_action_at is None

    # the job was cancelled: the endpoints refuse, so there is no action, and the
    # core does not wait for one (it ends the cancel instead)
    _flag_cancel(sessions, operation.parent_job_id, clock.now)
    without_action = _after_the_observation_budget(
        adapter, sessions, clock, operation.id
    )
    assert without_action.row.state is not State.NEEDS_OPERATOR
    fresh = jobs.enqueue(parent(sessions, clock).id, NODE_A, kind, COMMIT, payload)
    assert fresh.id != operation.id
    assert fresh.state == State.QUEUED


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
    assert "effect unknown" in (_stored(sessions, operation.id).status_reason or "")


def test_an_order_whose_job_ended_ends_with_it(agent_service) -> None:
    jobs, sessions, clock = agent_service
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(parent_job.id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD)
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    clock.advance(seconds=45)
    jobs.record_late_result(_restart_interrupted_result(claim, KIND_STOP))
    _park_like_a_legacy_wait(sessions, operation.id)
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
    assert "the effect is unobserved" in (stored.status_reason or "")


def test_many_legacy_parked_orders_heal_in_one_pass(agent_service) -> None:
    jobs, sessions, clock = agent_service
    ids = []
    for _ in range(3):
        operation = jobs.enqueue(
            parent(sessions, clock).id,
            NODE_A,
            KIND_DISTRIBUTION,
            COMMIT,
            {"plan_digest": COMMIT},
        )
        ids.append(operation.id)
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    clock.advance(seconds=45)
    for operation_id in ids:
        _park_like_a_legacy_wait(sessions, operation_id)
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
    _park_like_a_legacy_wait(sessions, operation.id)

    reconciler = jobs._order_reconciler()
    store = reconciler._store
    (row,) = store.due(clock.now, 10)
    decision = _decision_for(jobs, sessions, clock, operation.id)
    # An operator resumes the order between the read and the write.
    with sessions.begin() as session:
        session.get(AgentOperation, operation.id).next_action_at = clock.now
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
        ([("waiting-for-operator", True), ("succeeded", False)], False, "queued"),
        ([("waiting-for-operator", True)], True, None),
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

        # The row a Controller wrote before the rename carries the cause in the word.
        attempt.state = "expired"
        attempt.observation_cause = None
        session.flush()
        assert aos.attempt_lapsed(attempt) and aos.attempt_is_observing(attempt)
        assert (
            attempt.id
            in session.scalars(
                select(AgentOperationAttempt.id).where(
                    aos.sql_attempt_lapsed(AgentOperationAttempt)
                )
            ).all()
        )
        attempt.state = "waiting-for-operator"
        session.flush()
        assert aos.attempt_reported_unknown(attempt) and not aos.attempt_lapsed(attempt)
        assert (
            attempt.id
            in session.scalars(
                select(AgentOperationAttempt.id).where(
                    aos.sql_attempt_failed_or_unknown(AgentOperationAttempt)
                )
            ).all()
        )
        session.rollback()


def test_a_legacy_parked_order_is_found_by_every_selection_and_a_new_one_too(
    agent_service,
) -> None:
    jobs, sessions, clock = agent_service
    order = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, KIND_STOP, COMMIT, STOP_PAYLOAD
    )
    assert claim_agent(jobs, NODE_A, "serial-a") is not None
    for word in (
        "waiting-for-operator",
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
