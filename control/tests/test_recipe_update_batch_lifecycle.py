"""A recipe update batch decides through the lifecycle core.

The batch is a composite over its children: it follows them, never waits for an
operator, its cancel always completes, and legacy rows are adopted without
touching a live claim.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from vonk_agent_protocol import LifecycleState
from vonk_control.lifecycle import (
    CancelRequested,
    Outcome,
    Reported,
    State,
    Tick,
    transition,
)
from vonk_control.lifecycle.recipe_update_batch import (
    CANCEL_BUDGET,
    RecipeUpdateBatchAdapter,
)
from vonk_control.models import Job
from vonk_control.recipe_image_availability import RecipeImageAvailabilityError
from vonk_control.recipe_update_contract import (
    UPDATE_KIND,
    RecipeUpdateChild,
    RecipeUpdateDocument,
    RecipeUpdateScope,
)

from .test_recipe_update_batches import _start, update_env  # noqa: F401

NOW = datetime(2026, 9, 1, tzinfo=UTC)
STORED = (
    "queued",
    "running",
    "cancelling",
    "waiting-for-operator",
    "succeeded",
    "partial",
    "failed",
    "cancelled",
)


def _job(state: str) -> Job:
    return Job(
        id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        kind=UPDATE_KIND,
        state=state,
        actor="a",
        authority_revision="r",
        targets=[],
        payload_digest="d",
        payload={},
        current_attempt=1,
        created_at=NOW,
        updated_at=NOW,
    )


def _child(state: Any, operation: bool = True, index: int = 0) -> RecipeUpdateChild:
    return RecipeUpdateChild(
        recipe_revision_id=str(uuid.uuid4()),
        recipe_content_sha256="a" * 64,
        effective_execution_key="b" * 64,
        recipe_name=f"p/s{index}",
        request_key=str(uuid.uuid4()),
        operation_id=str(uuid.uuid4()) if operation else None,
        state=state,
    )


def _document(*states: str) -> RecipeUpdateDocument:
    return RecipeUpdateDocument(
        request=RecipeUpdateScope(selectors=["x"], all=False),
        children=[_child(state, index=index) for index, state in enumerate(states)],
    )


@pytest.mark.parametrize("stored", STORED)
def test_no_event_leaves_a_batch_waiting_for_an_operator(stored) -> None:
    adapter = RecipeUpdateBatchAdapter()
    row = adapter.lifecycle(_job(stored), None, NOW)
    for event in (
        Tick(),
        CancelRequested("k"),
        Reported(Outcome.UNKNOWN),
        Reported(Outcome.FAILED, retryable=True),
    ):
        after = transition(row, event, adapter, NOW).row
        # a legacy wait is only ever re-evaluated: it is never produced
        assert after.state is not State.NEEDS_OPERATOR or row.state is (
            State.NEEDS_OPERATOR
        )
    assert adapter.actions(row) == ()
    assert not adapter.irreversible(row)


@pytest.mark.parametrize(
    ("children", "stored"),
    [
        (("succeeded", "succeeded"), "succeeded"),
        (("succeeded", "failed"), "failed"),
        (("succeeded", "cancelled"), "failed"),
        (("failed", "failed"), "failed"),
        (("cancelled",), "failed"),
    ],
)
def test_the_worst_child_outcome_ends_the_batch(children, stored) -> None:
    adapter = RecipeUpdateBatchAdapter()
    job = _job("running")
    assert adapter.conclude(job, _document(*children), NOW) is not None
    assert job.state == stored


def test_a_batch_with_a_child_that_has_not_settled_does_not_end() -> None:
    adapter = RecipeUpdateBatchAdapter()
    job = _job("running")
    assert adapter.conclude(job, _document("succeeded", "running"), NOW) is None
    assert adapter.conclude(job, _document("succeeded", "partial"), NOW) is None
    assert job.state == "running"


def test_a_cancel_ends_when_every_child_settled() -> None:
    adapter = RecipeUpdateBatchAdapter()
    job = _job("cancelling")
    row = adapter.cancel_progress(
        job, None, NOW, settled_children=True, reason="stop it"
    )
    assert row.state is State.CANCELLED and job.state == "cancelled"


def test_a_cancel_that_cannot_confirm_ends_after_the_stop_budget() -> None:
    adapter = RecipeUpdateBatchAdapter()
    document = _document("running")
    job = _job("cancelling")
    job.updated_at = NOW
    from vonk_control.recipe_lifecycle_contract import (
        RecipeOperationCancellationResult,
    )

    document.cancellation = RecipeOperationCancellationResult(
        cancel_requested=True,
        cancel_requested_at=NOW,
        cancel_request_id=str(uuid.uuid4()),
        cancel_actor="op",
        reason="stop it",
    )
    adapter.cancel_progress(
        job,
        document,
        NOW + timedelta(seconds=30),
        settled_children=False,
        reason="stop it",
    )
    assert job.state == "observing"  # still within the budget
    adapter.cancel_progress(
        job,
        document,
        NOW + CANCEL_BUDGET + timedelta(seconds=1),
        settled_children=False,
        reason="stop it",
    )
    assert job.state == "cancelled"


def test_a_cancel_that_a_child_will_not_confirm_completes(update_env) -> None:  # noqa: F811
    sessions, _recipes, now, fresh = update_env
    service = fresh()
    parent = _start(service, list(_recipes)[:1])
    claim = service.claim_update(owner="w")
    with sessions() as session:
        row = session.get(Job, parent.id)
        document = service._updates._document(row)
        intent = service._updates._intent(document.children[0])
        key = document.children[0].request_key
    child = service._start_request(
        intent, actor="operator", request_id=key, update_claim=claim
    )
    service.cancel(
        parent.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000a11",
        reason="stop it",
    )

    def refuse(*_args, **_kwargs):
        raise RecipeImageAvailabilityError("recipe_image.unavailable", "no")

    service._cancel_update_child = refuse
    service.reconcile_cancellations()
    assert service.get_operator_operation(parent.id).state == LifecycleState.OBSERVING
    now[0] += CANCEL_BUDGET + timedelta(seconds=5)
    service.reconcile_cancellations()
    observed = service.get_operator_operation(parent.id)
    assert observed.state == "cancelled"
    assert observed.children[0].state != "succeeded"
    # the child keeps its own lifecycle and identity
    assert service.get(child.id).id == child.id


@pytest.mark.usefixtures("damaged_json_rows")
def test_an_unreadable_cancel_document_still_completes(update_env) -> None:  # noqa: F811
    sessions, recipes, _now, fresh = update_env
    service = fresh()
    parent = _start(service, list(recipes)[:1])
    service.cancel(
        parent.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000a12",
        reason="stop it",
    )
    with sessions.begin() as session:
        session.get(Job, parent.id).payload = {"invalid": True}
    service.reconcile_cancellations()
    with sessions() as session:
        row = session.get(Job, parent.id)
        assert row.state == "cancelled"
        assert row.payload == {"invalid": True}  # the evidence is retained


def test_a_legacy_running_batch_with_no_claim_is_claimed_and_a_live_one_is_not(
    update_env,  # noqa: F811
) -> None:
    sessions, recipes, now, fresh = update_env
    service = fresh()
    parent = _start(service, list(recipes)[:1])
    first = service.claim_update(owner="w1")
    assert first is not None
    with sessions() as session:
        assert session.get(Job, parent.id).state == "running"
        assert session.get(Job, parent.id).current_attempt == 1
    # a live claim is never touched
    assert service.claim_update(owner="w2") is None
    # the lease lapses (the worker died): the next pass claims it again
    now[0] += timedelta(seconds=11)
    second = service.claim_update(owner="w2")
    assert second is not None and second.owner != first.owner
    with sessions() as session:
        assert session.get(Job, parent.id).current_attempt == 2


def test_a_cancelled_batch_is_not_claimed(update_env) -> None:  # noqa: F811
    _sessions, recipes, _now, fresh = update_env
    service = fresh()
    parent = _start(service, list(recipes)[:1])
    service.cancel(
        parent.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000a13",
        reason="stop it",
    )
    assert service.claim_update(owner="w") is None
