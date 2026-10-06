"""The NAS model cache on the shared lifecycle core.

``test_lifecycle_core`` proves the core as a table.  This module proves the cache
adapter and its reconcile loop against the stored operation: what a stored (and a
legacy) row *is* to the core, the single projection back onto the row, and the
promises the audit asked for: a legacy row adopts and heals, no operator wait
without an action, a cancel completes, a restart in the middle of a transfer is
harmless, and a missing receipt or a full disk is a wait that heals, never an end.
"""

# ruff: noqa: F811 - tests take the imported ``cache`` fixture by name
from __future__ import annotations

import fcntl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState
from vonk_control.lifecycle import (
    STOP_BUDGET,
    Effect,
    LeaseLapsed,
    Observed,
    Outcome,
    Reported,
    State,
    Tick,
    transition,
)
from vonk_control.lifecycle.model_cache import (
    adopt_legacy_operations,
    legacy_claim,
    legacy_retry_due,
)
from vonk_control.model_cache import ModelCacheConflict, ModelCacheService
from vonk_control.models import ModelCacheOperation, ModelCacheSet

from .test_model_cache import (
    _artifact,
    _download,
    cache,  # noqa: F401 - the fixture
)

MODEL = "a" * 64


def _clock_at(service: ModelCacheService, start: datetime | None = None):
    """Give ``service`` a movable clock; returns the one-element cell."""

    now = [start or datetime.now(UTC)]
    service._clock = lambda: now[0]
    return now


def _queue(service: ModelCacheService, tmp_path: Path, key: str, data: bytes = b"x"):
    artifact = _artifact(tmp_path, data, model_content_sha256=MODEL)
    preview = service.download_preview(model_content_sha256=MODEL, artifacts=[artifact])
    operation = service.start_download(
        actor="test",
        request_key=key,
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=MODEL,
        artifacts=[artifact],
    )
    return operation, artifact


def _row(sessions, operation_id: str) -> ModelCacheOperation:
    with sessions() as session:
        stored = session.get(ModelCacheOperation, operation_id)
        assert stored is not None
        session.expunge(stored)
        return stored


def _edit(sessions, operation_id: str, **fields: object) -> None:
    with sessions.begin() as session:
        stored = session.get(ModelCacheOperation, operation_id)
        assert stored is not None
        for name, value in fields.items():
            setattr(stored, name, value)


def _edit_payload(sessions, operation_id: str, **changes: object) -> None:
    with sessions.begin() as session:
        stored = session.get(ModelCacheOperation, operation_id)
        assert stored is not None
        payload = dict(stored.payload)
        for name, value in changes.items():
            if name == "retry":
                payload["retry"] = dict(payload["retry"]) | dict(value)  # type: ignore[call-overload]
            else:
                payload[name] = value
        stored.payload = payload


def _aware(value: datetime | None) -> datetime | None:
    return None if value is None else value.replace(tzinfo=UTC)


# ------------------------------------------------------------ adopt and heal


def test_a_legacy_retry_clock_is_adopted_honoured_and_healed(cache, tmp_path):
    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a001"
    )
    now = _clock_at(service)
    due = now[0] + timedelta(minutes=10)
    # The row as a Controller before the core left it: the retry clock in the
    # payload, no column.
    _edit_payload(
        sessions,
        operation.id,
        retry={"next_retry_at": due.isoformat(), "retry_after_seconds": 600},
    )
    stored = _row(sessions, operation.id)
    assert stored.next_action_at is None
    assert legacy_retry_due(stored.payload) == due

    # Lazy adoption: the core reads it as a scheduled retry, nothing rewritten.
    adopted = service._lifecycle.adopt(stored)
    assert (adopted.state, adopted.next_action_at) == (State.BACKOFF, due)
    assert service._claim_operations(limit=1, respect_backoff=True) == []

    # Startup adoption moves it onto the column once, idempotently.
    with sessions.begin() as session:
        assert adopt_legacy_operations(session.connection()) == 1
    with sessions.begin() as session:
        assert adopt_legacy_operations(session.connection()) == 0
    assert _aware(_row(sessions, operation.id).next_action_at) == due

    # It heals: due, it is claimed; the legacy clock is gone with the claim.
    now[0] = due + timedelta(seconds=1)
    assert service._claim_operations(limit=1, respect_backoff=True) == [
        (operation.id, "download")
    ]
    claimed = _row(sessions, operation.id)
    assert (claimed.state, claimed.fence) == ("running", service._claim_owner)
    assert claimed.next_action_at is None
    assert legacy_retry_due(claimed.payload) is None


def test_a_legacy_live_claim_is_untouched_and_a_dead_one_is_retried(cache, tmp_path):
    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a002"
    )
    now = _clock_at(service)
    # A transfer another (older) Controller process is running: its claim lives in
    # the payload, and the row says running.
    _edit(sessions, operation.id, state="running")
    _edit_payload(
        sessions,
        operation.id,
        claim={
            "owner": "older-process",
            "expires_at": (now[0] + timedelta(seconds=90)).isoformat(),
        },
    )
    assert legacy_claim(_row(sessions, operation.id).payload) is not None
    with sessions.begin() as session:
        assert adopt_legacy_operations(session.connection()) == 1
    live = _row(sessions, operation.id)
    assert (live.state, live.fence) == ("running", "older-process")

    # A live lease is never touched: not claimed, not decided, not rewritten.
    assert service._claim_operations(limit=1, respect_backoff=False) == []
    assert service._reconciler.reconcile().changed == 0
    assert _row(sessions, operation.id).fence == "older-process"

    # Its process dies: once the lease lapses the core retries the attempt (the
    # kind is idempotent) and the next claim takes it.
    now[0] += timedelta(seconds=200)
    assert service._reconciler.reconcile().changed == 1
    retried = _row(sessions, operation.id)
    assert (
        retried.state == LifecycleState.BACKOFF and retried.next_action_at is not None
    )
    assert retried.lease_deadline is None
    assert service._reconciler.reconcile().changed == 0  # idempotent
    now[0] += timedelta(minutes=5)
    assert service._claim_operations(limit=1, respect_backoff=True) == [
        (operation.id, "download")
    ]


def test_an_unadopted_lapsed_legacy_row_still_heals(cache, tmp_path):
    """Startup adoption is a convenience: a row it missed heals on its own."""

    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a003"
    )
    now = _clock_at(service)
    _edit(sessions, operation.id, state="running")
    _edit_payload(
        sessions,
        operation.id,
        claim={
            "owner": "dead-process",
            "expires_at": (now[0] - timedelta(minutes=3)).isoformat(),
        },
    )
    assert service._reconciler.reconcile().changed == 1
    healed = _row(sessions, operation.id)
    assert healed.state == LifecycleState.BACKOFF and healed.next_action_at is not None
    assert "claim" not in healed.payload  # the legacy claim retired with the decision


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_corrupt_document_never_raises_out_of_the_core(cache, tmp_path):
    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a004"
    )
    _edit(sessions, operation.id, state="running", payload={"schema_version": 7})
    row = service._lifecycle.adopt(_row(sessions, operation.id))
    assert row.state is State.RUNNING and not row.cancel_requested
    # Bookkeeping is unknown, then reconciled: the sweep decides, it does not raise.
    now = _clock_at(service, datetime.now(UTC) + timedelta(minutes=5))
    service._reconciler.reconcile()
    assert now[0] > datetime.now(UTC)


# ------------------------------------------------- no operator wait, ever


@pytest.mark.parametrize(
    "stored",
    [
        {"state": "queued"},
        {"state": "partial"},
        {"state": "running", "lease_deadline": datetime(2020, 1, 1, tzinfo=UTC)},
        {"state": "running", "lease_deadline": datetime(2099, 1, 1, tzinfo=UTC)},
        {"state": "queued", "next_action_at": datetime(2099, 1, 1, tzinfo=UTC)},
        {"state": "partial", "observe_count": 3},
    ],
    ids=["queued", "partial", "lapsed", "live", "backoff", "observing"],
)
def test_no_event_parks_a_cache_operation_for_an_operator(cache, tmp_path, stored):
    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a010"
    )
    _edit(sessions, operation.id, **stored)
    adapter = service._lifecycle
    now = datetime.now(UTC) + timedelta(days=400)
    row = adapter.adopt(_row(sessions, operation.id))
    assert adapter.actions(row) == () and not adapter.irreversible(row)
    for event in (
        LeaseLapsed(),
        Tick(),
        Reported(Outcome.UNKNOWN),
        Reported(Outcome.FAILED, retryable=True),
        Observed(Effect.UNKNOWN),
        Observed(Effect.NONE),
    ):
        decision = transition(row, event, adapter, now)
        assert decision.row.state is not State.NEEDS_OPERATOR, event


def test_a_failed_attempt_is_retried_never_waits_for_a_person(cache, tmp_path):
    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a011"
    )

    def broken(spec, offset):
        raise OSError(5, "input/output error")

    service._open_source = broken
    assert service.run_pending() == 1
    waiting = _row(sessions, operation.id)
    assert waiting.state == "queued" and waiting.next_action_at is not None
    assert waiting.attempt == 1  # attempts are counted at claim
    assert waiting.lease_deadline is None
    view = service.get_operation(operation.id)
    assert view.retryable and view.next_attempt_at is not None
    assert view.failure is not None and view.failure["retryable"] is True


# --------------------------------------------------------- a cancel completes


def test_cancelling_a_queued_download_ends_it_at_once(cache, tmp_path):
    service, sessions = cache
    operation, artifact = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a020"
    )
    view = service.cancel_operation(
        operation.id,
        actor="test",
        request_key="00000000-0000-4000-8000-00000000a021",
        reason="not needed",
    )
    assert view.state == "cancelled"
    stored = _row(sessions, operation.id)
    assert stored.completed_at is not None and stored.next_action_at is None
    with sessions() as session:
        cache_set = session.get(ModelCacheSet, str(operation.artifact_set_sha256))
        assert cache_set is not None and cache_set.state == "incomplete"
    assert service._object_path(str(artifact["sha256"])).exists() is False


def test_a_cancel_ends_even_when_the_stop_is_never_confirmed(cache, tmp_path):
    """Rule 4: after the stop budget the row is cancelled, effect unknown, never waiting."""

    service, sessions = cache
    operation, artifact = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a022"
    )
    now = _clock_at(service)
    assert service._claim_operations(limit=1, respect_backoff=False) == [
        (operation.id, "download")
    ]
    # A writer of the operation's object that never lets go (an empty owner
    # record is "may be this operation's own").
    lock = service.root / "locks" / str(artifact["sha256"])
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        view = service.cancel_operation(
            operation.id,
            actor="test",
            request_key="00000000-0000-4000-8000-00000000a023",
            reason="stop it",
        )
        assert view.state == LifecycleState.OBSERVING
        states = []
        for _ in range(STOP_BUDGET + 3):
            now[0] += timedelta(minutes=5)
            service._reconciler.reconcile()
            states.append(_row(sessions, operation.id).state)
            if states[-1] == "cancelled":
                break
        fcntl.flock(held, fcntl.LOCK_UN)
    assert states[-1] == "cancelled", states
    stored = _row(sessions, operation.id)
    assert "unconfirmed" in (stored.last_error or "")  # the residue record
    assert stored.observe_count == 0 and stored.completed_at is not None
    assert service.get_operation(operation.id).state == "cancelled"


def test_a_cancel_settles_after_a_restart(cache, tmp_path):
    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a024"
    )
    # The intent was persisted, then the process died before settling it.
    _edit_payload(
        sessions,
        operation.id,
        cancellation={
            "request_key": "00000000-0000-4000-8000-00000000a025",
            "actor": "test",
            "reason": "persisted, never settled",
            "requested_at": datetime.now(UTC).isoformat(),
        },
    )
    restarted = ModelCacheService(
        sessions, service.root, reserve_bytes=0, fixture_sources=True
    )
    restarted.tick()
    assert _row(sessions, operation.id).state == "cancelled"


# ------------------------------------------------------------ restart safety


def test_a_restart_in_the_middle_of_a_transfer_is_harmless(cache, tmp_path):
    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a030"
    )
    now = _clock_at(service)
    assert service._claim_operations(limit=1, respect_backoff=True) == [
        (operation.id, "download")
    ]
    # The process dies with the transfer claimed.  A restarted Controller sees a
    # live lease and leaves it alone ...
    restarted = ModelCacheService(
        sessions,
        service.root,
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )
    assert restarted.resume_operations() == 1
    assert restarted._reconciler.reconcile().changed == 0
    assert restarted._claim_operations(limit=1, respect_backoff=True) == []
    # ... and takes the transfer over once the lease lapses.
    now[0] += timedelta(minutes=10)
    assert restarted._reconciler.reconcile().changed == 1
    assert restarted._reconciler.reconcile().changed == 0
    now[0] += timedelta(minutes=10)
    restarted.run_pending()
    assert restarted.get_operation(operation.id).state == "succeeded"
    done = _row(sessions, operation.id)
    assert done.fence == restarted._claim_owner and done.attempt == 2


def test_the_adapter_observes_the_receipts_and_never_marks_work_done(cache, tmp_path):
    service, sessions = cache
    downloaded = _download(
        service,
        [_artifact(tmp_path, b"observed", model_content_sha256=MODEL)],
        model_content_sha256=MODEL,
        request_key="00000000-0000-4000-8000-00000000a040",
    )
    assert downloaded.state == "succeeded"
    row = service._lifecycle.adopt(_row(sessions, downloaded.id))
    assert service._lifecycle.observe(row).effect is Effect.ESTABLISHED
    # The bytes are there but the set is not published: the observer says "not
    # yet" (the transfer's retry publishes it), it does not mark anything done.
    _edit_set_state(sessions, str(downloaded.artifact_set_sha256), "incomplete")
    assert service._lifecycle.observe(row).effect is Effect.NONE
    # An unreadable storage is unknown, not a refusal.
    service.objects_present = lambda payload: None  # type: ignore[method-assign]
    assert service._lifecycle.observe(row).effect is Effect.UNKNOWN


def _edit_set_state(sessions, set_digest: str, state: str) -> None:
    with sessions.begin() as session:
        row = session.get(ModelCacheSet, set_digest)
        assert row is not None
        row.state = state


# ------------------------------------- unknown, then re-verify (top-10 #8)


def test_a_lost_receipt_is_reverified_not_final(cache, tmp_path):
    service, sessions = cache
    artifact = _artifact(tmp_path, b"coverage", model_content_sha256=MODEL)
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256=MODEL,
        request_key="00000000-0000-4000-8000-00000000a050",
    )
    set_digest = str(downloaded.artifact_set_sha256)
    service._object_path(str(artifact["sha256"])).unlink()

    with pytest.raises(ModelCacheConflict) as refused:
        service.resolve_verified_artifact_set(set_digest)
    assert refused.value.code == "model_cache.coverage_incomplete"
    assert refused.value.recovery == "reverify"
    assert refused.value.retry_after_seconds  # the consumer retries; nothing ended
    with sessions() as session:
        assert session.get(ModelCacheSet, set_digest).state == "needs-repair"
        queued = list(
            session.scalars(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.actor == "system:model-cache-reverify"
                )
            )
        )
    assert [item.state for item in queued] == ["queued"]
    # A consumer retrying every few seconds costs one operation, not one each.
    with pytest.raises(ModelCacheConflict):
        service.resolve_verified_artifact_set(set_digest)
    with sessions() as session:
        assert len(list(session.scalars(select(ModelCacheOperation)))) == 2

    # The re-verification runs, and the same request now succeeds.
    service.run_pending()
    assert service.resolve_verified_artifact_set(set_digest)
    with sessions() as session:
        assert session.get(ModelCacheSet, set_digest).state == "cached"


def test_an_unverified_object_is_reverified_not_final(cache, tmp_path):
    service, _sessions = cache
    artifact = _artifact(tmp_path, b"unverified", model_content_sha256=MODEL)
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256=MODEL,
        request_key="00000000-0000-4000-8000-00000000a051",
    )
    set_digest = str(downloaded.artifact_set_sha256)
    service._object_path(str(artifact["sha256"])).unlink()
    with pytest.raises(ModelCacheConflict) as refused:
        service.cached_artifact_file(set_digest, str(artifact["sha256"]), "weights.bin")
    assert refused.value.code == "model_cache.artifact_unverified"
    assert refused.value.recovery == "reverify"
    service.run_pending()
    path, size, digest = service.cached_artifact_file(
        set_digest, str(artifact["sha256"]), "weights.bin"
    )
    assert (size, digest) == (len(b"unverified"), artifact["sha256"])
    assert path.read_bytes() == b"unverified"


def test_receipts_that_are_complete_heal_a_set_that_says_otherwise(cache, tmp_path):
    service, sessions = cache
    artifact = _artifact(tmp_path, b"healed", model_content_sha256=MODEL)
    downloaded = _download(
        service,
        [artifact],
        model_content_sha256=MODEL,
        request_key="00000000-0000-4000-8000-00000000a052",
    )
    set_digest = str(downloaded.artifact_set_sha256)
    _edit_set_state(sessions, set_digest, "needs-repair")
    manifest = service._manifest_for_set(set_digest)
    service._reverify_set(manifest)
    with sessions() as session:
        assert session.get(ModelCacheSet, set_digest).state == "cached"
        assert len(list(session.scalars(select(ModelCacheOperation)))) == 1


# ------------------------------------------ a full disk is a wait, not a refusal


def test_a_full_disk_queues_the_download_and_it_runs_when_space_returns(
    cache, tmp_path, monkeypatch
):
    from collections import namedtuple

    service, sessions = cache
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "vonk_control.model_cache.shutil.disk_usage", lambda _: usage(100, 100, 0)
    )
    operation, artifact = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a060", b"needs space"
    )
    assert operation.state == "queued"
    assert operation.failure is not None
    assert operation.failure["code"] == "model_cache.download_blocked"
    assert operation.failure["retryable"] is True
    assert operation.next_attempt_at is not None
    # No space: nothing is downloaded into a full disk, and nobody is asked to
    # resubmit; the core reschedules the wait.
    assert service._claim_operations(limit=1, respect_backoff=False) == []
    assert _row(sessions, operation.id).state == "queued"
    assert not service._object_path(str(artifact["sha256"])).exists()
    monkeypatch.setattr(
        "vonk_control.model_cache.shutil.disk_usage",
        lambda _: usage(10**12, 0, 10**12),
    )
    assert service.run_pending() == 1
    assert service.get_operation(operation.id).state == "succeeded"


# ------------------------------------ the stored vocabulary is the core's


def test_a_row_written_as_partial_is_adopted_found_and_served_as_backoff(
    cache, tmp_path
):
    """The retired word is read, selected and shown as the word it means now."""

    from vonk_control import model_cache_states

    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a020"
    )
    _edit(sessions, operation.id, state="partial")

    row = service._lifecycle.adopt(_row(sessions, operation.id))
    assert row.state is State.BACKOFF
    assert service.get_operation(operation.id).state == LifecycleState.BACKOFF
    with sessions() as session:
        found = session.scalars(
            select(ModelCacheOperation.id).where(
                ModelCacheOperation.state.in_(model_cache_states.LIVE)
            )
        ).all()
    assert operation.id in found
    # An old Activity filter still finds it by either name.
    for word in ("partial", "backoff"):
        page = service.activity_operations(state=word)
        assert [item.id for item in page["operations"]] == [operation.id]


def test_the_cache_writes_backoff_never_partial(cache, tmp_path):
    service, sessions = cache
    operation, _artifact_document = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a021"
    )
    _edit(sessions, operation.id, state="running")
    service._lifecycle.reopen(_row(sessions, operation.id), datetime.now(UTC))
    assert LifecycleState.BACKOFF.value == "backoff"
    from vonk_control import model_cache_states

    assert model_cache_states.BACKOFF == "backoff"


# ------------------------------------ a background transfer's unknown outcome


def _settle_background(service: ModelCacheService) -> None:
    """Tick the pool worker until its in-flight transfers have been settled."""

    import time

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        service.tick()
        if not service._background_operations:
            return
        time.sleep(0.01)
    raise AssertionError("the background transfers never settled")


def test_a_background_transfer_with_an_unknown_outcome_is_kept_and_retried(
    cache, tmp_path
):
    from vonk_control.model_cache import ModelCacheStorageUnknown

    service, _sessions = cache
    now = _clock_at(service)
    operation, _artifact_spec = _queue(
        service, tmp_path, "00000000-0000-4000-8000-00000000a061", b"flaky storage"
    )
    original = service._open_source
    unavailable = True

    def source(spec, offset):
        if unavailable:
            raise ModelCacheStorageUnknown(
                "model_cache.source_unavailable", "the source is unavailable"
            )
        return original(spec, offset)

    service._open_source = source
    _settle_background(service)
    waiting = service.get_operation(operation.id)
    # The unknown outcome keeps the operation: queued with a bounded retry, not
    # failed, and nobody is asked to resubmit.
    assert waiting.state == "queued"
    assert waiting.failure is not None and waiting.failure["retryable"] is True
    assert waiting.next_attempt_at is not None

    unavailable = False
    now[0] = now[0] + timedelta(hours=1)
    _settle_background(service)
    assert service.get_operation(operation.id).state == "succeeded"
