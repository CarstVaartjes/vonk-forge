"""Request recovery must release execution ownership and preserve exact content."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState, ProgressPhase
from vonk_control.job_documents import AvailabilityJobPayload
from vonk_control.lifecycle.image_availability import CANCEL_BUDGET, PREPARATION_BUDGET
from vonk_control.models import Base, Job, User
from vonk_control.recipe_image_availability.contracts import OPERATION_KIND
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_control.strict_json import serialize_json_value

from .test_recipe_image_availability import (
    Transport,
    _add_revision,
    _availability_payload,
    _recipe,
    _runtime,
    _service,
)


def _owner(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'requests.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = [datetime.now(UTC)]
    recipe = _recipe("recipe-source-build.json")
    with sessions.begin() as session:
        _add_revision(session, "request-revision", recipe)
        _add_revision(session, "unrelated-revision", recipe)
        session.add(User(subject="operator", role="operator"))
    transport = Transport()
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
        authority=lambda *args, **kwargs: (recipe, _runtime()),
        clock=lambda: now[0],
        transport=transport,
    )
    return engine, sessions, service, now, transport


@pytest.mark.parametrize("history_depth", [65, 130])
def test_deep_history_has_no_admission_or_cancellation_barrier(tmp_path, history_depth):
    """Catch restarting every ensure/cancel at the same capped history prefix."""
    engine, sessions, service, now, _ = _owner(tmp_path)
    request = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL, "vonk-forge:profile-preparation:request-revision"
        )
    )
    payload = _availability_payload("request-revision")
    old = now[0] - timedelta(hours=1)
    with sessions.begin() as session:
        for index in range(history_depth):
            job_id = str(uuid.uuid4())
            session.add(
                Job(
                    id=job_id,
                    request_id=request,
                    kind=OPERATION_KIND,
                    state=LifecycleState.CANCELLED.value,
                    actor="operator",
                    authority_revision="request-revision",
                    targets=["request-revision"],
                    payload_digest="a" * 64,
                    payload=serialize_json_value(payload),
                    current_attempt=0,
                    created_at=old + timedelta(microseconds=index),
                    updated_at=old + timedelta(microseconds=index),
                )
            )
            request = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"vonk-forge:profile-preparation:request-revision:{job_id}",
                )
            )
    assert service.ensure_preparation("request-revision", actor="operator")
    with sessions() as session:
        current = session.scalar(select(Job).where(Job.request_id == request))
        assert current is not None and current.state == LifecycleState.QUEUED.value
        current_id = current.id
    assert service.cancel_profile_preparation(
        "request-revision", actor="operator", reason="selected application ended"
    ) == (current_id,)
    assert service.get(current_id).state == LifecycleState.CANCELLED.value
    fresh = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert fresh.id != current_id
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact is not None
    engine.dispose()


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_queue_window_cannot_hide_healthy_content(tmp_path):
    """Catch skipping unreadable queue heads forever instead of fencing them."""
    engine, sessions, service, now, _ = _owner(tmp_path)
    with sessions.begin() as session:
        for index in range(300):
            session.add(
                Job(
                    id=str(uuid.uuid4()),
                    request_id=str(uuid.uuid4()),
                    kind=OPERATION_KIND,
                    state=LifecycleState.QUEUED.value,
                    actor="operator",
                    authority_revision="damaged-revision",
                    targets=[],
                    payload_digest="a" * 64,
                    payload={},
                    current_attempt=0,
                    created_at=now[0] - timedelta(minutes=1),
                    updated_at=now[0] - timedelta(seconds=300 - index),
                )
            )
    healthy = service.start(
        "unrelated-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    for _ in range(3):
        service.run_pending()
    assert service.get(healthy.id).artifact is not None
    with sessions() as session:
        assert not tuple(
            session.scalars(
                select(Job.id).where(
                    Job.authority_revision == "damaged-revision",
                    Job.state == LifecycleState.QUEUED.value,
                )
            )
        )
    fresh = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact is not None
    engine.dispose()


@pytest.mark.parametrize("issued", [False, True])
def test_same_revision_force_supersedes_old_claim_and_late_effects(tmp_path, issued):
    """Catch revision ordering and live-lease exemptions from newer intent."""
    engine, sessions, service, now, transport = _owner(tmp_path)
    older = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    claim = service.claim_pending(limit=1)[0] if issued else None
    newer = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4()), force=True
    )
    if claim is not None:
        service.run_claim(claim)
        assert transport.calls == 0
    service.reconcile_cancellations()
    now[0] += CANCEL_BUDGET + timedelta(seconds=1)
    service.reconcile_cancellations()
    ended = service.get(older.id)
    assert ended.state == LifecycleState.CANCELLED.value
    assert ended.artifact is None
    with sessions() as session:
        row = session.get(Job, older.id)
        assert row is not None
        payload = service._payload(row)
        assert isinstance(payload, AvailabilityJobPayload)
        assert payload.claim_owner is None and payload.claim_until is None
    assert service.run_pending() == 1
    assert service.get(newer.id).artifact is not None
    fresh = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert fresh.state == LifecycleState.QUEUED.value
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact is not None
    engine.dispose()


def test_busy_identity_releases_claim_then_reuses_exact_verified_content(tmp_path):
    """Catch a worker blocking on an identity lock while renewing its lease."""
    engine, sessions, service, now, transport = _owner(tmp_path)
    parent = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    claim = service.claim_pending(limit=1)[0]
    lock = service._identity_lock("f" * 64)
    assert lock.acquire(blocking=False)
    try:
        service.run_claim(claim)
    finally:
        lock.release()
    assert transport.calls == 0
    with sessions() as session:
        row = session.get(Job, parent.id)
        assert row is not None
        payload = service._payload(row)
        assert isinstance(payload, AvailabilityJobPayload)
        assert payload.claim_owner is None and payload.claim_until is None
        assert payload.retry_after_at is not None
    # Another request can prepare the content during the contender's backoff.
    unrelated = service.start(
        "unrelated-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    expected = service.get(unrelated.id).artifact
    assert expected is not None
    now[0] += timedelta(minutes=1)
    assert service.run_pending() == 1
    assert service.get(parent.id).artifact == expected
    assert transport.calls == 1
    fresh = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact == expected
    engine.dispose()


def test_live_lease_cannot_extend_request_lifetime_or_accept_late_publication(tmp_path):
    """Catch heartbeat renewal defeating the immutable preparation deadline."""
    engine, _sessions, service, now, transport = _owner(tmp_path)
    parent = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    claim = service.claim_pending(limit=1)[0]
    now[0] += PREPARATION_BUDGET + timedelta(seconds=1)
    assert service._renew_claim(claim) is False
    assert service.claim_pending(limit=1) == ()
    service.run_claim(claim)
    assert transport.calls == 0
    now[0] += CANCEL_BUDGET + timedelta(seconds=1)
    service.reconcile_cancellations()
    ended = service.get(parent.id)
    assert ended.state == LifecycleState.CANCELLED.value
    assert ended.artifact is None
    fresh = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact is not None
    engine.dispose()


@pytest.mark.parametrize("state", [LifecycleState.BACKOFF, LifecycleState.OBSERVING])
def test_new_request_supersedes_parked_verified_image_without_losing_bytes(
    tmp_path, state
):
    """Catch durable image results exempting obsolete parents from supersession."""
    engine, sessions, service, now, _ = _owner(tmp_path)
    older = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    verified = service.get(older.id).artifact
    assert verified is not None
    with sessions.begin() as session:
        row = session.get(Job, older.id)
        assert row is not None
        row.state = LifecycleState.BACKOFF.value
        row.result = None
        payload = service._payload(row)
        assert isinstance(payload, AvailabilityJobPayload)
        assert payload.image_result is not None
    if state is LifecycleState.OBSERVING:
        service.cancel(
            older.id,
            actor="operator",
            request_id=str(uuid.uuid4()),
            reason="application ended",
        )
    newer = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4()), force=True
    )
    now[0] += CANCEL_BUDGET + timedelta(seconds=1)
    service.reconcile_cancellations()
    assert service.get(older.id).state == LifecycleState.CANCELLED.value
    assert service._storage.build_archive_available(
        verified.oci_archive_sha256, verified.image_bytes
    )
    assert service.run_pending() == 1
    assert service.get(newer.id).artifact is not None
    fresh = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact is not None
    engine.dispose()


def test_lost_model_projection_is_reconciled_by_same_request_and_reuses_image(tmp_path):
    """Catch missing-child observations being overridden into permanent failures."""
    from types import SimpleNamespace

    from vonk_control.model_cache import ModelCacheNotFound
    from vonk_control.model_cache_contract import ModelCacheCounters
    from vonk_control.model_cache_progress import cache_progress, progress_document
    from vonk_control.models import ModelCacheOperation

    from .test_recipe_image_availability import (
        _download_preview,
        _manifest,
        _persist_fake_model_cache_child,
    )

    engine, sessions, service, now, transport = _owner(tmp_path)
    model_digest = next(
        model.model.content_sha256
        for model in _recipe("recipe-source-build.json").models
    )
    child = SimpleNamespace(
        id=str(uuid.uuid4()),
        request_key=str(uuid.uuid4()),
        state=LifecycleState.RUNNING.value,
        artifact_set_sha256="c" * 64,
        plan_digest="d" * 64,
        failure=None,
        progress=progress_document(
            cache_progress(
                ModelCacheCounters(
                    phase=ProgressPhase.DOWNLOADING.value,
                    downloaded_bytes=40,
                    expected_bytes=100,
                    completed_artifacts=0,
                    total_artifacts=1,
                ),
                previous=None,
                now=now[0],
            )
        ),
    )
    requests: list[str] = []

    class CacheOwner:
        def download_preview(self, **kwargs):
            return _download_preview(
                plan_digest=child.plan_digest,
                artifact_set_sha256=child.artifact_set_sha256,
                new_bytes=0,
            )

        def resolve_artifact_set(self, **kwargs):
            return SimpleNamespace(
                digest=child.artifact_set_sha256,
                document=lambda: _manifest(model_content_digests=[model_digest]),
            )

        def list_operations(self, **kwargs):
            return ()

        def start_download(self, *, request_key, **kwargs):
            requests.append(request_key)
            child.request_key = request_key
            _persist_fake_model_cache_child(
                sessions, child=child, model_content_sha256=model_digest, now=now[0]
            )
            return child

        def get_operation(self, operation_id):
            with sessions() as session:
                if session.get(ModelCacheOperation, operation_id) is None:
                    raise ModelCacheNotFound(
                        "model_cache.operation_missing", operation_id
                    )
            return child

    service._model_cache = CacheOwner()
    parent = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    assert transport.calls == 1
    with sessions.begin() as session:
        row = session.get(ModelCacheOperation, child.id)
        assert row is not None
        session.delete(row)
    child.state = LifecycleState.SUCCEEDED.value
    child.progress = progress_document(
        cache_progress(
            ModelCacheCounters(
                phase=ProgressPhase.COMPLETED.value,
                downloaded_bytes=100,
                expected_bytes=100,
                completed_artifacts=1,
                total_artifacts=1,
            ),
            previous=None,
            now=now[0],
        )
    )
    now[0] += timedelta(seconds=6)
    assert service.run_pending() == 1
    completed = service.get(parent.id)
    assert completed.artifact is not None
    assert completed.model_preparation is not None
    assert completed.model_preparation.id == child.id
    assert len(requests) == 2 and requests[0] == requests[1]
    assert transport.calls == 1
    fresh = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact is not None
    assert transport.calls == 1
    engine.dispose()


def test_child_observation_fault_cannot_skip_cancellation_deadline_or_healthy_work(
    tmp_path, monkeypatch
):
    """Catch an exception bypassing expiry and aborting all later queue claims."""
    engine, sessions, service, now, _ = _owner(tmp_path)
    older = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    service.claim_pending(limit=1)
    service.cancel(
        older.id,
        actor="operator",
        request_id=str(uuid.uuid4()),
        reason="application ended",
    )

    def unavailable(operation_id):
        raise OSError("child owner reply is unavailable")

    monkeypatch.setattr(service, "_reconcile_cancellation_pass", unavailable)
    healthy = service.start(
        "unrelated-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    assert service.get(healthy.id).artifact is not None
    now[0] += CANCEL_BUDGET + timedelta(seconds=1)
    service.reconcile_cancellations()
    assert service.get(older.id).state == LifecycleState.CANCELLED.value
    with sessions() as session:
        row = session.get(Job, older.id)
        assert row is not None
        payload = service._payload(row)
        assert isinstance(payload, AvailabilityJobPayload)
        assert payload.claim_owner is None and payload.claim_until is None
    fresh = service.start(
        "request-revision", actor="operator", request_id=str(uuid.uuid4())
    )
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact is not None
    engine.dispose()
