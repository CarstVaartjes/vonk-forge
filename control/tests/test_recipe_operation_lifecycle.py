"""A recipe operation (the parent ``Job`` of ``recipe.*``) on the shared lifecycle core.

The parent is a composite over its Spark orders, so it is never irreversible and
never waits for an operator itself.  These tests prove the adapter's rules over
stored rows (what a stored and a legacy parent *is* to the core, that a cancel
always completes, that a build whose completion raced its cancel stays cancelling
instead of waiting, that a legacy wait heals) and the service's use of it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from vonk_control.lifecycle import Effect, State, transition
from vonk_control.lifecycle.recipe_operation import RecipeOperationAdapter
from vonk_control.lifecycle.types import Observed, Tick
from vonk_control.models import AgentOperation, Job, RecipeBuild
from vonk_control.recipe_operations import RecipeOperationConflict

from .test_build_cancellation_recovery import _issue, _services

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
ADAPTER = RecipeOperationAdapter()


def _job(state: str = "running", **result) -> Job:
    return Job(
        id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        kind="recipe.install",
        state=state,
        actor="operator",
        authority_revision="r",
        targets=["node"],
        payload_digest="d",
        payload={},
        result=result or None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_a_parent_is_created_live_or_already_done_never_parked() -> None:
    for state in ("queued", "running", "succeeded"):
        assert RecipeOperationAdapter.new_job(**_fields(state)).state == state
    for state in ("waiting-for-operator", "failed", "cancelled"):
        with pytest.raises(ValueError):
            RecipeOperationAdapter.new_job(**_fields(state))


def _fields(state: str) -> dict:
    job = _job(state)
    return {
        column: getattr(job, column)
        for column in (
            "id",
            "request_id",
            "kind",
            "state",
            "actor",
            "authority_revision",
            "targets",
            "payload_digest",
            "payload",
            "created_at",
            "updated_at",
        )
    }


def test_adopt_maps_every_stored_state_and_the_cancel_request() -> None:
    expected = {
        "queued": State.QUEUED,
        "running": State.RUNNING,
        "succeeded": State.SUCCEEDED,
        "failed": State.FAILED,
        "cancelled": State.CANCELLED,
        "waiting-for-operator": State.NEEDS_OPERATOR,
        "something-legacy": State.NEEDS_OPERATOR,
    }
    for stored, state in expected.items():
        assert ADAPTER.adopt(_job(stored)).state is state
    flagged = ADAPTER.adopt(
        _job(
            cancel_requested=True,
            cancel_request_id="k",
            cancel_requested_at=NOW.isoformat(),
        )
    )
    assert flagged.cancel_requested_at == NOW and flagged.cancel_request_key == "k"


def test_a_parent_whose_orders_never_ran_is_cancelled_at_once() -> None:
    job = _job("queued")
    row = ADAPTER.request_cancel(
        job, NOW, issued=False, request_key="k", reason="not needed"
    )
    assert row.state is State.CANCELLED and job.state == "cancelled"
    fresh = _job(State.QUEUED)
    assert ADAPTER.adopt(fresh).state is State.QUEUED
    assert fresh.request_id != job.request_id


def test_a_cancel_of_issued_orders_stays_in_flight_never_waits() -> None:
    job = _job("running")
    row = ADAPTER.request_cancel(job, NOW, issued=True, request_key="k", reason="stop")
    assert row.state is State.OBSERVING and row.cancel_requested
    assert job.state == "running"  # the orders complete it, bounded by their core


def test_no_event_leaves_a_parent_waiting_for_an_operator() -> None:
    for stored in ("queued", "running", "waiting-for-operator", "x"):
        for issued in (False, True):
            job = _job(stored)
            row = ADAPTER.lifecycle(job, issued=issued, now=NOW)
            for event in (Tick(), Observed(Effect.UNKNOWN)):
                after = transition(row, event, ADAPTER, NOW).row
                assert after.state is not State.NEEDS_OPERATOR
    assert ADAPTER.actions(ADAPTER.adopt(_job("waiting-for-operator"))) == ()


def test_a_build_completion_that_raced_its_cancel_keeps_cancelling() -> None:
    job = _job("running", cancel_requested=True)
    ADAPTER.cancel_race(job, NOW)
    assert job.state == "running"  # audit C7: never waiting-for-operator
    ADAPTER.cancelled(job, NOW)
    assert job.state == "cancelled"


def test_a_legacy_build_wait_with_a_cancel_heals_and_one_without_is_left() -> None:
    parked = _job("waiting-for-operator", cancel_requested=True)
    assert ADAPTER.heal(parked, NOW) is True and parked.state == "running"
    other = _job("waiting-for-operator")
    assert ADAPTER.heal(other, NOW) is False and other.state == "waiting-for-operator"


def test_a_definite_end_is_absorbed_by_a_parent_that_already_ended() -> None:
    job = _job("cancelled")
    ADAPTER.finish(job, NOW, failed=False)
    assert job.state == "cancelled"
    ADAPTER.project(job, NOW)
    assert job.state == "cancelled"


def test_finish_records_the_definite_outcome_of_the_orders() -> None:
    ok, bad = _job("running"), _job("running")
    ADAPTER.finish(ok, NOW, failed=False)
    ADAPTER.finish(bad, NOW, failed=True)
    assert (ok.state, bad.state) == ("succeeded", "failed")


def test_a_build_completion_racing_a_cancel_is_swept_not_parked(
    tmp_path, postgres_engine
) -> None:
    sessions, _builds, operations, _storage, _now, _node, _revision, plan = _services(
        tmp_path, postgres_engine
    )
    original = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    _issue(sessions, original.id)
    operations.cancel(
        original.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="cancel",
    )
    # The legacy shape an older Controller left: the parent waits for a person.
    with sessions.begin() as session:
        job = session.get(Job, original.id)
        assert job is not None
        job.state = "waiting-for-operator"
    operations.reconcile_cancelled_builds()
    with sessions() as session:
        job = session.get(Job, original.id)
        assert job is not None
        assert job.state != "waiting-for-operator"
        assert job.state in {"running", "cancelled"}
        assert session.get(RecipeBuild, plan.build_id) is not None


def test_a_waiting_parent_accepts_a_cancel(tmp_path, postgres_engine) -> None:
    sessions, _builds, operations, _storage, _now, _node, _revision, plan = _services(
        tmp_path, postgres_engine
    )
    original = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    child_id = _issue(sessions, original.id)
    with sessions.begin() as session:
        parked = session.get(Job, original.id)
        child = session.get(AgentOperation, child_id)
        assert parked is not None and child is not None
        parked.state = "waiting-for-operator"
        child.state = "waiting-for-operator"
    try:
        operations.cancel(
            original.id,
            actor="operator",
            request_id=str(uuid.uuid4()),
            reason="stop it",
        )
    except RecipeOperationConflict as error:  # pragma: no cover - the old refusal
        pytest.fail(f"a cancel is always accepted: {error}")
    with sessions() as session:
        job = session.get(Job, original.id)
        assert job is not None
        assert job.result is not None and job.result["cancel_requested"] is True
