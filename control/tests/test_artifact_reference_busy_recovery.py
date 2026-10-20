"""Reference contention waits and re-plans through the durable download owner."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, current_thread
from typing import cast

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    Disposition,
    LifecycleState,
    RecipeImageCode,
)
from vonk_control.artifact_lifecycle import ArtifactReferenceUnsettled
from vonk_control.failure_classification import disposition
from vonk_control.job_documents import AvailabilityJobPayload
from vonk_control.lifecycle.image_availability import preparation_deadline
from vonk_control.models import Base, Job
from vonk_control.operation_contract import AvailabilityRecoveryAction
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityService,
    image_preparation,
)
from vonk_control.recipe_image_availability.contracts import availability_disposition
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_control.unused_storage_collection import UnusedStorageCollector
from vonk_control.unused_storage_collection.common import InstallationRemoval, _Kept

from .test_recipe_image_availability import (
    ARCHIVE_SHA,
    _add_head,
    _add_revision,
    _recipe,
    _reference_receipt,
    _runtime,
    _service,
)
from .test_unused_storage_collection import FakeLifecycle


def _download(tmp_path: Path, engine: Engine):
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    recipe = _recipe("recipe-source-build.json")
    with sessions.begin() as session:
        revision = _add_revision(session, "reference-contention", recipe)
        _add_head(session, revision)
    now = [datetime.now(UTC)]
    storage = FilesystemRuntimeImageStorage(tmp_path)

    def reopen() -> RecipeImageAvailabilityService:
        return _service(
            sessions,
            storage=storage,
            authority=lambda *_args, **_kwargs: (recipe, _runtime()),
            clock=lambda: now[0],
        )

    service = reopen()
    queued = service.start(revision.id, actor="operator", request_id="refbusy")
    return sessions, storage, now, reopen, service, queued


@pytest.mark.parametrize("provisional", [True, False])
@pytest.mark.parametrize(
    "code",
    [
        code
        for code in ArtifactLifecycleCode
        if code.value.endswith(("_busy", "_conflict"))
    ]
    + [
        ArtifactLifecycleCode.REFERENCE_CHANGED,
        ArtifactLifecycleCode.DELETION_IN_PROGRESS,
    ],
)
def test_every_reference_busy_or_ownership_conflict_stays_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: ArtifactLifecycleCode,
    provisional: bool,
) -> None:
    """Catches adapters turning a typed conflict into a security refusal,
    or the transient error family defaulting to retryable=False."""
    engine = create_engine(f"sqlite:///{tmp_path / 'control.sqlite'}")
    _, _, _, _, service, _ = _download(tmp_path, engine)
    claim = service.claim_pending()[0]

    def busy(*_args, **_kwargs) -> None:
        raise ArtifactReferenceUnsettled(code, "concurrent reference owner")

    monkeypatch.setattr(image_preparation, "require_reference_open", busy)
    with pytest.raises(Exception) as raised:
        if provisional:
            service._persist_provisional_image_reference(
                claim, receipt=_reference_receipt()
            )
        else:
            service._persist_receipt(claim, _reference_receipt())
    assert disposition(raised.value) is Disposition.WAIT
    assert availability_disposition(raised.value) is Disposition.WAIT
    assert isinstance(raised.value, ArtifactReferenceUnsettled)
    assert raised.value.retryable is True


def test_collection_racing_download_converges_after_restart(
    tmp_path: Path, postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a real collection gate ending an accepted download terminally.

    Pause the collection transaction after acquiring its advisory gate. The
    download must release its SQL claim and worker slot, retain its verified
    image and retry clock, then finish under the same request after restart.
    """
    sessions, storage, now, reopen, service, queued = _download(
        tmp_path, postgres_engine
    )
    collector = UnusedStorageCollector(
        sessions,
        clock=lambda: now[0],
        lifecycle=cast(InstallationRemoval, FakeLifecycle(sessions)),
        image_cache_root=tmp_path,
    )
    held, release = Event(), Event()
    receipt_path = storage.root / f"{ARCHIVE_SHA}.receipt.json"

    def pause_collection(_conn, _cursor, statement, _parameters, _context, _many):
        if (
            current_thread().name.startswith("refbusy-collection")
            and "pg_try_advisory_xact_lock" in statement
            and not held.is_set()
        ):
            held.set()
            assert release.wait(timeout=20)

    def collect() -> None:
        # The recheck retains the receipt now named by the live download.
        with pytest.raises(_Kept):
            collector._remove_receipt(ARCHIVE_SHA, receipt_path)

    persist = service._persist_receipt
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="refbusy-collection"
    ) as pool:
        event.listen(postgres_engine, "after_cursor_execute", pause_collection)
        futures = []

        def race_collection(claim, receipt):
            futures.append(pool.submit(collect))
            assert held.wait(timeout=20)
            persist(claim, receipt)

        monkeypatch.setattr(service, "_persist_receipt", race_collection)
        try:
            assert service.run_pending() == 1
            waiting = service.get(queued.id)
            assert waiting.state == LifecycleState.QUEUED
            assert waiting.failure_evidence is not None
            assert waiting.failure_evidence.code == ArtifactLifecycleCode.REFERENCE_BUSY
            assert waiting.failure_evidence.retryable is True
            assert (
                AvailabilityRecoveryAction.RETRY
                in waiting.failure_evidence.recovery_actions
            )
            assert waiting.next_attempt_at is not None
            assert waiting.wait is not None
            assert waiting.wait.dependency_deadline is not None
            deadline = waiting.wait.dependency_deadline
            assert waiting.wait.next_check is not None
            with sessions() as session:
                row = session.get(Job, queued.id)
                assert row is not None
                payload = AvailabilityJobPayload.model_validate_json(
                    json.dumps(row.payload)
                )
                assert payload.claim_owner is None and payload.claim_until is None
                assert payload.retry.automatic_attempts == 0
                assert deadline == preparation_deadline(
                    row.created_at, payload.progress_advanced_at
                )
            assert receipt_path.is_file()
        finally:
            release.set()
            for future in futures:
                future.result(timeout=20)
            event.remove(postgres_engine, "after_cursor_execute", pause_collection)
    restarted = reopen()
    assert restarted.get(queued.id).next_attempt_at == waiting.next_attempt_at
    assert restarted.run_pending() == 0
    now[0] = datetime.fromisoformat(waiting.next_attempt_at)
    assert restarted.run_pending() == 1
    finished = restarted.get(queued.id)
    assert finished.state == LifecycleState.SUCCEEDED, finished.failure_evidence
    assert finished.request_id == queued.request_id
    assert finished.failure_evidence is None and finished.wait is None
    assert finished.artifact is not None
    assert finished.artifact.oci_archive_sha256 == ARCHIVE_SHA


def test_busy_wait_deadline_survives_restart_and_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches waiting forever or granting a fresh window on each retry."""
    engine = create_engine(f"sqlite:///{tmp_path / 'control.sqlite'}")
    _, _, now, reopen, service, queued = _download(tmp_path, engine)

    def busy(*_args, **_kwargs) -> None:
        raise ArtifactReferenceUnsettled(
            ArtifactLifecycleCode.REFERENCE_BUSY, "concurrent reference owner"
        )

    monkeypatch.setattr(image_preparation, "require_reference_open", busy)
    assert service.run_pending() == 1
    waiting = service.get(queued.id)
    assert waiting.wait is not None and waiting.wait.dependency_deadline is not None
    deadline = waiting.wait.dependency_deadline
    assert waiting.next_attempt_at is not None
    now[0] = datetime.fromisoformat(waiting.next_attempt_at)
    restarted = reopen()
    assert restarted.run_pending() == 1
    waiting_again = restarted.get(queued.id)
    assert waiting_again.wait is not None
    assert waiting_again.wait.dependency_deadline == deadline
    now[0] = deadline
    restarted = reopen()
    assert restarted.claim_pending() == ()
    ended = restarted.get(queued.id)
    assert ended.state in {LifecycleState.OBSERVING, LifecycleState.CANCELLED}
    assert ended.failure_evidence is not None
    assert ended.failure_evidence.code == RecipeImageCode.PREPARATION_EXHAUSTED
