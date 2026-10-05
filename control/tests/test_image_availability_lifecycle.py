"""Recipe image availability on the shared lifecycle core.

An availability preparation, its update-batch parent's cancel, a recipe cache
removal and the prebuilt-image import are ``Job`` rows whose state is written only
by ``ImageAvailabilityAdapter`` / ``PrebuiltImageAdapter``.  This module proves the
rules against stored rows: what a stored (and a legacy) row *is* to the core, that
nothing ever waits for an operator (the kinds have no action to advertise), that a
failure is retried at the core's clock with the owner's delay as a floor or ends
when the owner types it definite, that a cancel always completes (at once for work
that never ran, and past a claim that never releases once the budget is spent),
and that rows an older Controller left heal without touching live work.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.lifecycle import (
    STOP_BUDGET,
    CancelRequested,
    Lifecycle,
    Outcome,
    Reported,
    State,
    Tick,
    transition,
)
from vonk_control.lifecycle.image_availability import (
    CANCEL_BUDGET,
    OPERATION_KIND,
    REMOVAL_KIND,
    ImageAvailabilityAdapter,
    PrebuiltImageAdapter,
)
from vonk_control.models import Base, Job, User
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.recipe_update_contract import UPDATE_KIND
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .test_recipe_image_availability import (
    Transport,
    _add_revision,
    _recipe,
    _runtime,
    _service,
)

NOW = datetime(2026, 10, 5, 12, tzinfo=UTC)
STORED = ("queued", "running", "partial", "cancelling", "waiting-for-operator")


def _job(kind: str = OPERATION_KIND, state: str = "queued", **payload) -> Job:
    return Job(
        id="job-1",
        request_id="r" * 36,
        kind=kind,
        state=state,
        actor="operator",
        authority_revision="revision",
        targets=["revision"],
        payload_digest="0" * 64,
        payload=dict(payload),
        current_attempt=int(payload.pop("attempt", 0)) if "attempt" in payload else 0,
        created_at=NOW,
        updated_at=NOW,
    )


def _adapter() -> ImageAvailabilityAdapter:
    return ImageAvailabilityAdapter(clock=lambda: NOW)


# ------------------------------------------------------------------- adopt


def test_every_stored_shape_adopts_without_a_wait() -> None:
    adapter = _adapter()
    live = (NOW + timedelta(minutes=1)).isoformat()
    lapsed = (NOW - timedelta(minutes=1)).isoformat()
    cases = {
        ("queued", ()): State.QUEUED,
        ("running", (("claim_until", live),)): State.RUNNING,
        ("running", (("claim_until", lapsed),)): State.BACKOFF,
        ("running", ()): State.BACKOFF,
        ("partial", ()): State.BACKOFF,
        ("cancelling", ()): State.OBSERVING,
        ("succeeded", ()): State.SUCCEEDED,
        ("failed", ()): State.FAILED,
        ("cancelled", ()): State.CANCELLED,
        ("waiting-for-operator", ()): State.NEEDS_OPERATOR,
    }
    for (stored, extra), expected in cases.items():
        row = adapter.adopt(_job(state=stored, **dict(extra)))
        assert row.state is expected, (stored, extra)
    # A retry clock in the future is a backoff, in the past it is claimable now.
    future = (NOW + timedelta(minutes=5)).isoformat()
    assert adapter.adopt(_job(retry_after_at=future)).state is State.BACKOFF
    assert adapter.adopt(_job(retry_after_at=lapsed)).state is State.QUEUED


def test_a_live_lease_is_never_lapsed_by_adoption() -> None:
    live = (NOW + timedelta(seconds=30)).isoformat()
    row = _adapter().adopt(_job(state="running", claim_until=live, claim_owner="w"))
    assert row.state is State.RUNNING and row.fence == "w"
    assert row.lease_deadline == datetime.fromisoformat(live)


def test_the_cancel_budget_is_derived_from_the_recorded_request() -> None:
    adapter = _adapter()
    young = {"cancel_requested_at": NOW.isoformat(), "cancel_request_id": "k"}
    old = {
        "cancel_requested_at": (NOW - CANCEL_BUDGET).isoformat(),
        "cancel_request_id": "k",
    }
    assert (
        adapter.adopt(_job(state="cancelling", cancellation=young)).observe_count == 0
    )
    spent = adapter.adopt(_job(state="cancelling", cancellation=old))
    assert spent.observe_count >= STOP_BUDGET and spent.cancel_requested


# ------------------------------------------------- no operator wait, ever


@pytest.mark.parametrize("kind", [OPERATION_KIND, REMOVAL_KIND, UPDATE_KIND])
def test_no_stored_state_and_event_leaves_a_row_waiting_for_a_person(kind: str) -> None:
    adapter = _adapter()
    events = [
        Tick(),
        CancelRequested("k", "stop"),
        Reported(Outcome.UNKNOWN),
        Reported(Outcome.FAILED, retryable=True),
        Reported(Outcome.FAILED, retryable=False),
        Reported(Outcome.DONE),
    ]
    for stored, event in product(STORED, events):
        row: Lifecycle = adapter.adopt(_job(kind, stored))
        for _ in range(3):
            row = transition(row, event, adapter, NOW).row
            assert row.state is not State.NEEDS_OPERATOR or stored == (
                "waiting-for-operator"
            ), (kind, stored, event)
    assert adapter.actions(adapter.adopt(_job(kind, "queued"))) == ()


def test_a_legacy_operator_wait_is_retried_not_kept() -> None:
    adapter = _adapter()
    row = adapter.adopt(_job(state="waiting-for-operator"))
    healed = transition(row, Tick(), adapter, NOW).row
    assert healed.state is State.BACKOFF  # rule 1: idempotent work is retried


# --------------------------------------------------------- failure and retry


def test_a_retryable_failure_is_retried_at_the_cores_clock_with_the_floor() -> None:
    adapter = _adapter()
    job = _job(
        state="running",
        claim_owner="w",
        attempt=1,
        claim_until=(NOW + timedelta(minutes=1)).isoformat(),
    )
    floor = NOW + timedelta(seconds=45)
    payload = dict(job.payload)
    after = adapter.fail(
        job,
        NOW,
        retryable=True,
        reason="boom",
        retry_after=floor,
        payload=payload,
        count=0,
    )
    assert after.state is State.BACKOFF and job.state == "queued"
    assert job.status_reason == "boom"
    due = datetime.fromisoformat(str(payload["retry_after_at"]))
    assert due >= floor and due <= floor + timedelta(seconds=90)


def test_a_definite_failure_ends_and_a_removal_failure_is_partial() -> None:
    adapter = _adapter()
    prep = _job(state="running", attempt=1)
    assert (
        adapter.fail(prep, NOW, retryable=False, reason="invalid").state is State.FAILED
    )
    assert prep.state == "failed"
    removal = _job(REMOVAL_KIND, "running", attempt=1)
    after = adapter.fail(removal, NOW, retryable=True, reason="busy", count=2)
    assert after.state is State.BACKOFF and removal.state == "partial"
    assert after.next_action_at is not None and after.next_action_at > NOW


def test_a_dependency_wait_is_partial_and_does_not_count_as_a_failure() -> None:
    adapter = _adapter()
    job = _job(
        state="running",
        claim_owner="w",
        attempt=1,
        claim_until=(NOW + timedelta(minutes=1)).isoformat(),
    )
    payload = dict(job.payload)
    after = adapter.defer(job, NOW, NOW + timedelta(seconds=1), payload=payload)
    assert job.state == "partial" and after.state is State.BACKOFF
    assert "retry_after_at" in payload


def test_claim_takes_a_lapsed_running_and_a_legacy_parked_row() -> None:
    adapter = _adapter()
    for stored in ("running", "partial", "waiting-for-operator", "queued"):
        job = _job(
            state=stored,
            attempt=1,
            retry_after_at=(NOW - timedelta(seconds=1)).isoformat(),
        )
        row = adapter.claim(job, "w2", NOW + timedelta(minutes=2), NOW)
        assert row.state is State.RUNNING and job.state == "running", stored
        assert job.current_attempt == 2


def test_removal_steps_run_and_a_retry_clock_never_blocks_the_next_step() -> None:
    adapter = _adapter()
    job = _job(REMOVAL_KIND, "partial", attempt=1)
    adapter.advance_removal(job, NOW)
    assert job.state == "running"
    adapter.advance_removal(job, NOW + timedelta(seconds=5))  # a step in a step
    assert job.state == "running"
    done = adapter.succeed(job, NOW)
    assert done.state is State.SUCCEEDED and job.state == "succeeded"


# ------------------------------------------------------------------- cancel


def test_a_cancel_of_work_that_never_ran_ends_at_once() -> None:
    adapter = _adapter()
    job = _job(state="queued")
    assert adapter.request_cancel(job, "k", "stop", NOW).state is State.CANCELLED
    assert job.state == "cancelled" and job.status_reason == "stop"


def test_a_cancel_of_issued_work_observes_then_ends_when_nothing_is_outstanding() -> (
    None
):
    adapter = _adapter()
    live = (NOW + timedelta(minutes=1)).isoformat()
    job = _job(state="running", claim_owner="w", claim_until=live, attempt=1)
    adapter.request_cancel(job, "k", "stop", NOW)
    assert job.state == "cancelling"
    job.payload = dict(job.payload) | {
        "cancellation": {
            "cancel_requested_at": NOW.isoformat(),
            "cancel_request_id": "k",
        }
    }
    still = adapter.settle_cancel(job, NOW, outstanding=True)
    assert still.state is State.OBSERVING and job.state == "cancelling"
    done = adapter.settle_cancel(job, NOW, outstanding=False)
    assert done.state is State.CANCELLED and job.state == "cancelled"


def test_an_update_batch_is_always_observed_by_its_owner() -> None:
    adapter = _adapter()
    job = _job(UPDATE_KIND, "queued")
    assert adapter.request_cancel(job, "k", "stop", NOW).state is State.OBSERVING
    assert job.state == "cancelling"


def test_a_supersede_ends_an_unstarted_preparation() -> None:
    adapter = _adapter()
    job = _job(state="queued")
    assert adapter.supersede(job, "newer revision", NOW).state is State.CANCELLED


def test_a_spent_cancel_ends_with_the_effect_unknown_and_fences_the_claim() -> None:
    adapter = ImageAvailabilityAdapter(clock=lambda: NOW + CANCEL_BUDGET)
    requested = {"cancel_requested_at": NOW.isoformat(), "cancel_request_id": "k"}
    job = _job(
        state="cancelling",
        attempt=1,
        claim_owner="gone",
        claim_until=(NOW + timedelta(hours=1)).isoformat(),
        cancellation=requested,
    )
    ended = adapter.settle_cancel(job, NOW + CANCEL_BUDGET, outstanding=True)
    assert ended.state is State.CANCELLED and ended.effect.value == "unknown"
    assert job.state == "cancelled" and "stayed unconfirmed" in str(job.status_reason)
    assert not job.payload.get("claim_owner") and not job.payload.get("claim_until")


# --------------------------------------------------------- the owner service


def _owned_service(tmp_path: Path, clock):
    engine = create_engine(f"sqlite:///{tmp_path / 'lifecycle.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    recipe = _recipe("recipe-source-build.json")
    with sessions.begin() as session:
        _add_revision(session, "revision-a", recipe)
        session.add(User(subject="operator", role="operator"))
    service: RecipeImageAvailabilityService = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        transport=Transport(),
        clock=clock,
    )
    return engine, sessions, service


def test_a_legacy_cancelling_preparation_with_a_stuck_claim_heals(
    tmp_path: Path,
) -> None:
    now = [NOW]
    engine, sessions, service = _owned_service(tmp_path, lambda: now[0])
    queued = service.start("revision-a", actor="operator", request_id="a" * 36)
    # What an older Controller left: cancelling, its claim never released and its
    # lease long lapsed owner that no process holds, with a child that never stops.
    with sessions.begin() as session:
        job = session.get(Job, queued.id)
        assert job is not None
        job.state = "cancelling"
        job.current_attempt = 1
        job.payload = dict(job.payload) | {
            "claim_owner": "dead-worker",
            "claim_until": (NOW + timedelta(days=1)).isoformat(),
            "cancellation": {
                "cancel_requested": True,
                "cancel_requested_at": NOW.isoformat(),
                "cancel_request_id": "00000000-0000-4000-8000-000000000a01",
                "cancel_actor": "operator",
                "reason": "legacy cancel",
            },
        }
    service.reconcile_cancellations()
    with sessions() as session:
        stuck = session.get(Job, queued.id)
        assert stuck is not None and stuck.state == "cancelling"  # inside the budget
    now[0] = NOW + CANCEL_BUDGET
    service.reconcile_cancellations()
    with sessions() as session:
        job = session.get(Job, queued.id)
        assert job is not None and job.state == "cancelled"
        assert not job.payload.get("claim_owner")
    engine.dispose()


def test_a_cancel_of_a_queued_preparation_via_the_service_completes(
    tmp_path: Path,
) -> None:
    engine, _sessions, service = _owned_service(tmp_path, lambda: NOW)
    queued = service.start("revision-a", actor="operator", request_id="b" * 36)
    view = service.cancel(
        queued.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000a02",
        reason="not needed",
    )
    assert view.state == "cancelled"
    assert service.run_pending() == 0
    engine.dispose()


def test_a_failed_preparation_is_requeued_and_claimed_again(tmp_path: Path) -> None:
    now = [NOW]
    engine, _sessions, service = _owned_service(tmp_path, lambda: now[0])
    calls = {"n": 0}

    class Flaky(Transport):
        def inspect_archive(self, archive, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("registry unavailable")
            return super().inspect_archive(archive, **kwargs)

    service._transport = Flaky()
    queued = service.start("revision-a", actor="operator", request_id="c" * 36)
    assert service.run_pending() == 1
    assert service.get(queued.id).state == "queued"
    assert service.run_pending() == 0  # the retry clock has not come
    now[0] += timedelta(minutes=5)
    assert service.run_pending() == 1
    assert service.get(queued.id).state == "succeeded"
    engine.dispose()


# ---------------------------------------------------------- prebuilt import


def test_the_prebuilt_import_ends_definitely_either_way() -> None:
    adapter = PrebuiltImageAdapter(clock=lambda: NOW)
    ok = _job(state="running", attempt=1)
    assert adapter.finish(ok, ok=True, now=NOW).state is State.SUCCEEDED
    assert ok.state == "succeeded"
    bad = _job(state="running", attempt=1)
    assert adapter.finish(bad, ok=False, now=NOW).state is State.FAILED
    assert bad.state == "failed"
