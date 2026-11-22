from __future__ import annotations

import json
import multiprocessing
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import LifecycleState
from vonk_control.bounded_json import require_mapping
from vonk_control.jobs.documents import (
    AvailabilityJobPayload,
    AvailabilityRuntime,
)
from vonk_control.model_cache_contract import (
    ModelCacheCounters,
)
from vonk_control.model_cache_progress import cache_progress, progress_document
from vonk_control.models import (
    ArtifactLifecycleGate,
    Base,
    CatalogDocumentRevision,
    Job,
    ModelCacheOperation,
    User,
)
from vonk_control.persisted_waits import schedule_wait
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
)
from vonk_control.recipe_image_availability_api import (
    _view_document,
)
from vonk_control.recipe_image_availability_view_contract import (
    RecipeCacheRemovalStatus,
    RecipeImageAvailabilityView,
)
from vonk_control.recipe_image_removal_contract import (
    RecipeCacheRemovalOwner,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImagePreparationError,
)
from vonk_control.strict_json import serialize_json_value
from vonk_forge_contracts import RecipeDefinition

from .non_blocking import assert_ended_without_blocking
from .recipe_image_availability_support import (
    ARCHIVE,
    ARCHIVE_SHA,
    Transport,
    _add_head,
    _add_revision,
    _archive_removal_service,
    _build_id,
    _build_runtime,
    _builder,
    _download_preview,
    _empty_recipe_removal_owner,
    _manifest,
    _persist_fake_model_cache_child,
    _progress_members,
    _recipe,
    _reference_receipt,
    _removal_owner,
    _runtime,
    _seed_archives,
    _service,
    _set_active_head,
    _successor,
)
from .recipe_removal_review_support import remove_after_review
from .runtime_image_fixtures import place_test_image, remove_test_image


class _ExitAfterImageUnlinkStorage(FilesystemRuntimeImageStorage):
    def remove_published(self, archive_sha256: str) -> int:
        super().remove_published(archive_sha256)
        os._exit(73)


def _exit_after_recipe_image_unlink(database_url: str, artifact_root: str) -> None:
    engine = create_engine(database_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = _service(
        sessions,
        storage=_ExitAfterImageUnlinkStorage(Path(artifact_root)),
        authority=lambda *_args, **_kwargs: (
            _recipe("recipe-source-build.json"),
            _runtime(),
        ),
        clock=lambda: datetime.now(UTC),
    )
    service.advance_removals(limit=1)
    os._exit(74)


def _add_recipe_successors(
    session: Session,
    *,
    older_id: str,
    older: RecipeDefinition,
    newer_id: str,
    newer: RecipeDefinition,
) -> tuple[CatalogDocumentRevision, CatalogDocumentRevision]:
    """Two revisions of one recipe document with the head on the older one."""

    older_revision = _add_revision(session, older_id, older)
    newer_revision = _add_revision(session, newer_id, newer)
    document_id = older_revision.document_id
    newer_revision.document_id = document_id
    newer_revision.revision_number = 2
    _add_head(session, older_revision)
    return older_revision, newer_revision


def test_postgres_recipe_removal_contended_archive_ends_the_batch_and_resumes(
    tmp_path: Path, postgres_engine
) -> None:
    """A held publication lock stops the batch after the archives done so far."""

    current = [datetime.now(UTC)]
    recipe = _recipe("recipe-source-build.json")
    service, sessions, storage = _archive_removal_service(
        postgres_engine, tmp_path, [recipe], lambda: current[0]
    )
    digests, receipts = _seed_archives(
        sessions, storage, "revision-contended", recipe, 3
    )
    request_id = str(uuid.uuid4())
    remove_after_review(
        service,
        recipe.identity.slug,
        actor="operator",
        request_id=request_id,
        with_model=False,
    )
    with sessions() as session:
        operation_id = session.scalar(
            select(Job.id).where(Job.request_id == request_id)
        )
    assert operation_id is not None
    # The scope and its fences are committed before any archive is touched.
    assert service._observe_recipe_removal(operation_id)
    with storage.publication_lock(digests[2]):
        service.advance_removals(limit=1)
        owner = _removal_owner(sessions, request_id)
        # The first two archives are checkpointed together; the third waits.
        assert owner.checkpoint.image_index == 2
        assert owner.checkpoint.failure is not None
        assert not (storage.root / f"{digests[1]}.receipt.json").exists()
        assert (storage.root / f"{digests[2]}.receipt.json").exists()
    current[0] += timedelta(minutes=1)
    for _ in range(5):
        service.advance_removals(limit=1)
    assert _removal_owner(sessions, request_id).checkpoint.image_index == 3
    assert not any(path.exists() for path in receipts)
    ended = service.get_operator_request(request_id, actor="operator")
    assert isinstance(ended, RecipeCacheRemovalStatus)
    assert ended.state == LifecycleState.SUCCEEDED
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cast(RecipeCacheRemovalStatus | RecipeImageAvailabilityView, ended),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "revision-contended",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )


def test_postgres_recipe_removal_recovers_after_process_death_between_unlink_and_checkpoint(
    tmp_path: Path, postgres_engine
) -> None:
    """An unlink lost to process death before its checkpoint is redone idempotently."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-source-build.json")
    receipt = _reference_receipt(_build_id("rev-removal-death"))
    now = datetime.now(UTC)
    with sessions.begin() as session:
        revision = _add_revision(session, "rev-removal-death", recipe)
        _add_head(session, revision)

    storage = FilesystemRuntimeImageStorage(tmp_path / "controller-artifacts")
    receipt_file = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
    receipt_file.write_text(
        json.dumps(receipt.model_dump(mode="json")), encoding="utf-8"
    )
    request_id = str(uuid.uuid4())
    service = _service(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now,
    )
    accepted = remove_after_review(
        service,
        recipe.identity.slug,
        actor="operator",
        request_id=request_id,
        with_model=False,
    )
    assert accepted["state"] == "queued"
    postgres_engine.dispose()

    process = multiprocessing.get_context("fork").Process(
        target=_exit_after_recipe_image_unlink,
        args=(
            postgres_engine.url.render_as_string(hide_password=False),
            str(storage.root.parent),
        ),
    )
    process.start()
    process.join(timeout=20)
    if process.is_alive():
        process.terminate()
        process.join(timeout=30)
        pytest.fail("recipe removal process did not reach its injected crash point")
    assert process.exitcode == 73
    # Only the receipt is retired; the blobs wait for garbage collection.
    assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()
    assert not receipt_file.exists()

    with sessions() as observer:
        owner_row = observer.scalar(select(Job).where(Job.request_id == request_id))
        assert owner_row is not None
        owner = RecipeCacheRemovalOwner.model_validate_json(
            json.dumps(owner_row.payload)
        )
        assert owner_row.state == "queued"
        assert owner_row.result is None
        assert owner.checkpoint.image_index == 0
        assert owner.checkpoint.image_reclaimed_bytes == 0

    restarted_storage = FilesystemRuntimeImageStorage(storage.root.parent)
    restarted = _service(
        sessions,
        storage=restarted_storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now,
    )
    assert restarted.advance_removals(limit=1) == 1
    assert restarted.advance_removals(limit=1) == 1
    recovered = restarted.get_operator_request(request_id, actor="operator")
    assert isinstance(recovered, RecipeCacheRemovalStatus)
    recovered = recovered.model_dump(mode="json", exclude_none=True)
    assert recovered["state"] == "succeeded"
    # The receipt was already gone when the restart measured, so the reclaimed
    # bytes are the manifest's, not the image's: an accepted display undercount.
    manifest = restarted_storage.layout.blob_path(f"sha256:{ARCHIVE_SHA}")
    assert recovered["reclaimed_bytes"] <= manifest.stat().st_size


def test_postgres_recipe_removal_retries_finalization_after_gate_contention(
    tmp_path: Path, postgres_engine
) -> None:
    """A due finalization retry clears prior contention and settles the owner."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-source-build.json")
    receipt = _reference_receipt(_build_id("rev-removal-finalize"))
    now = [datetime.now(UTC)]
    with sessions.begin() as session:
        revision = _add_revision(session, "rev-removal-finalize", recipe)
        _add_head(session, revision)
    storage = FilesystemRuntimeImageStorage(tmp_path / "controller-artifacts")
    place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
    (storage.root / f"{ARCHIVE_SHA}.receipt.json").write_text(
        json.dumps(receipt.model_dump(mode="json")), encoding="utf-8"
    )
    service = _service(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now[0],
    )
    request_key = str(uuid.uuid4())
    accepted = remove_after_review(
        service,
        recipe.identity.slug,
        actor="operator",
        request_id=request_key,
        with_model=False,
    )
    assert accepted["state"] == "queued"
    assert service.advance_removals(limit=1) == 1
    # Only the receipt is retired; the blobs wait for garbage collection.
    assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()

    with sessions.begin() as holder:
        locked_gate = holder.scalar(
            select(ArtifactLifecycleGate)
            .where(
                ArtifactLifecycleGate.artifact_kind == "runtime-image",
                ArtifactLifecycleGate.artifact_sha256 == ARCHIVE_SHA,
            )
            .with_for_update()
        )
        assert locked_gate is not None
        assert service.advance_removals(limit=1) == 1

    waiting = service.get_operator_request(request_key, actor="operator")
    assert isinstance(waiting, RecipeCacheRemovalStatus)
    waiting = waiting.model_dump(mode="json", exclude_none=True)
    assert waiting["state"] == LifecycleState.BACKOFF
    failure = require_mapping(waiting["failure"], "removal failure")
    assert failure["retryable"] is True
    retry_time = failure["retry_time"]
    assert isinstance(retry_time, str)
    now[0] = datetime.fromisoformat(retry_time) + timedelta(seconds=1)

    assert service.advance_removals(limit=1) == 1
    recovered = service.get_operator_request(request_key, actor="operator")
    assert isinstance(recovered, RecipeCacheRemovalStatus)
    recovered = recovered.model_dump(mode="json", exclude_none=True)
    assert recovered["state"] == "succeeded"


@pytest.mark.usefixtures("damaged_json_rows")
def test_recipe_removal_replay_rejects_malformed_stored_intent(
    tmp_path: Path,
) -> None:
    sessions, _, service, selector = _empty_recipe_removal_owner(tmp_path)
    request_id = "00000000-0000-4000-8000-000000000018"
    remove_after_review(
        service, selector, actor="operator", request_id=request_id, with_model=False
    )
    assert service.advance_removals(limit=1) == 1
    with sessions.begin() as session:
        operation = session.scalar(select(Job).where(Job.request_id == request_id))
        assert operation is not None
        malformed_payload = dict(require_mapping(operation.payload, "removal owner"))
        plan = dict(require_mapping(malformed_payload["plan"], "removal plan"))
        intent = dict(require_mapping(plan["intent"], "removal intent"))
        intent.pop("with_model")
        plan["intent"] = intent
        malformed_payload["plan"] = plan
        operation.payload = malformed_payload

    with pytest.raises(RecipeImageAvailabilityError):
        remove_after_review(
            service,
            selector,
            actor="operator",
            request_id=request_id,
            with_model=False,
        )

    with sessions() as session:
        ended = session.scalar(select(Job).where(Job.request_id == request_id))
        assert ended is not None and ended.state == "succeeded"
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cast(Job | RecipeImageAvailabilityView, ended),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "revision-image",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )


def test_recipe_removal_replay_rejects_issuer_drift_in_job_envelope(
    tmp_path: Path,
) -> None:
    sessions, _, service, selector = _empty_recipe_removal_owner(tmp_path)
    request_id = "00000000-0000-4000-8000-000000000019"
    remove_after_review(
        service, selector, actor="operator", request_id=request_id, with_model=False
    )
    assert service.advance_removals(limit=1) == 1
    with sessions.begin() as session:
        operation = session.scalar(select(Job).where(Job.request_id == request_id))
        assert operation is not None
        operation.actor = "another-issuer"

    with pytest.raises(RecipeImageAvailabilityError):
        remove_after_review(
            service,
            selector,
            actor="operator",
            request_id=request_id,
            with_model=False,
        )

    with sessions() as session:
        ended = session.scalar(select(Job).where(Job.request_id == request_id))
        assert ended is not None and ended.state == "succeeded"
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cast(Job | RecipeImageAvailabilityView, ended),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "revision-image",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )


def test_recipe_removal_replay_rebuilds_a_damaged_stored_result_from_its_checkpoint(
    tmp_path: Path,
) -> None:
    sessions, _, service, selector = _empty_recipe_removal_owner(tmp_path)
    request_id = "00000000-0000-4000-8000-000000000020"
    remove_after_review(
        service, selector, actor="operator", request_id=request_id, with_model=False
    )
    assert service.advance_removals(limit=1) == 1
    with sessions.begin() as session:
        operation = session.scalar(select(Job).where(Job.request_id == request_id))
        assert operation is not None and operation.result is not None
        malformed_result = dict(operation.result) | {"with_model": 0}
        session.execute(
            update(Job)
            .where(Job.request_id == request_id)
            .values(result=malformed_result)
        )
    with sessions() as session:
        operation = session.scalar(select(Job).where(Job.request_id == request_id))
        assert operation is not None and operation.result is not None
        stored_choice = operation.result["with_model"]
        assert isinstance(stored_choice, int) and not isinstance(stored_choice, bool)

    # The stored result is only a copy of what the owner's checkpoint says: a
    # damaged copy is projected again from that evidence, not a refused replay.
    replayed = remove_after_review(
        service,
        selector,
        actor="operator",
        request_id=request_id,
        with_model=False,
    )

    assert replayed["state"] == "succeeded"
    assert replayed["with_model"] is False
    assert replayed["operation_id"] == operation.id
    with sessions() as session:
        ended = session.scalar(select(Job).where(Job.request_id == request_id))
        assert ended is not None and ended.state == "succeeded"
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cast(Job | RecipeImageAvailabilityView, ended),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "revision-image",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )


@pytest.mark.parametrize(
    "wait_code",
    [
        "recipe_image.build_capacity_wait",
        "build.consumer_busy",
        "build.cancellation_pending",
    ],
)
def test_builder_dependency_wait_remains_durable_queue_after_automatic_limit(
    tmp_path: Path,
    wait_code: str,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-capacity-wait", recipe)

    def builder(*_: object, **__: object) -> dict[str, object]:
        raise RecipeImageAvailabilityError(
            wait_code,
            "build dependency has not settled",
            retryable=True,
            retry_after_seconds=1,
            recovery_actions=("resume", "retry"),
        )

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _build_runtime()),
        builder=builder,
        clock=lambda: datetime.now(UTC),
    )
    queued = service.start(
        "revision-capacity-wait", actor="operator", request_id="w" * 36
    )

    assert service.run_pending() == 1
    waiting = service.get(queued.id)
    assert waiting.state == "queued"
    assert waiting.failure is not None

    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        schedule_wait(operation, (datetime.now(UTC) - timedelta(seconds=1)))

    assert service.run_pending() == 1
    still_waiting = service.get(queued.id)
    assert still_waiting.state == "queued"
    assert still_waiting.attempt == 2
    assert still_waiting.failure is not None
    with sessions() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        retry = operation.payload["retry"]
        assert isinstance(retry, dict) and retry["automatic_attempts"] >= 1
        assert operation.payload.get("claim_owner") is None
        assert operation.payload.get("claim_until") is None


def test_failure_without_step_keeps_structured_retry_fields(tmp_path: Path) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-no-step", recipe)

    def builder(*_: object, **__: object) -> dict[str, object]:
        raise RecipeImageAvailabilityError(
            "model_cache.credentials_denied",
            "access remains denied",
            retryable=True,
            retry_time="2026-09-06T13:00:00+00:00",
            retry_after_seconds=60,
            recovery_actions=("check_access_and_resume",),
            log_excerpt="HF denied",
        )

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _build_runtime()),
        builder=builder,
        clock=lambda: datetime(2026, 9, 6, 12, tzinfo=UTC),
    )
    queued = service.start("revision-no-step", actor="operator", request_id="n" * 36)
    assert service.run_pending() == 1
    failed = service.get(queued.id)
    assert failed.failure is not None
    assert failed.failure["retry_time"] == "2026-09-06T13:00:00+00:00"
    assert failed.failure["recovery_actions"] == ["check_access_and_resume"]


def test_expired_claim_is_reclaimable_after_restart(tmp_path: Path) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-image", recipe)

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
        claim_lease_seconds=10,
    )
    queued = service.start("revision-image", actor="operator", request_id="2" * 36)
    claim = service.claim_pending(owner_id="worker-a")
    assert claim and claim[0].operation_id == queued.id
    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        operation.payload = dict(operation.payload) | {
            "claim_until": "2000-01-01T00:00:00+00:00"
        }
    reclaimed = service.claim_pending(owner_id="worker-b")
    assert reclaimed and reclaimed[0].claim_owner == "worker-b"


def test_claim_skips_backoff_and_renews_live_lease(tmp_path: Path) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-backoff", recipe)
    now = datetime.now(UTC)
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: now,
    )
    queued = service.start("revision-backoff", actor="operator", request_id="3" * 36)
    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        schedule_wait(operation, (now + timedelta(minutes=5)))
    assert service.claim_pending(owner_id="worker-a") == ()
    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        schedule_wait(operation, (now - timedelta(seconds=1)))

    claim = service.claim_pending(owner_id="worker-a")
    assert claim and claim[0].operation_id == queued.id
    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        before = operation.payload["claim_until"]
    assert service._renew_claim(claim[0]) is True
    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        assert operation.payload["claim_until"] == before


def test_claim_identity_uses_authoritative_image_and_running_claim_is_not_repeated(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-identity", recipe)
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
    )
    service.start("revision-identity", actor="operator", request_id="4" * 36)
    claim = service.claim_pending(owner_id="worker-a")
    assert claim and claim[0].build_input_sha256 == "f" * 64
    assert service.claim_pending(owner_id="worker-b") == ()


def test_publication_contention_reschedules_without_spending_transfer_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    now = [datetime.now(UTC)]
    with sessions.begin() as session:
        _add_revision(session, "publication-contention", recipe)
        session.add(User(subject="operator", role="operator"))
    storage = FilesystemRuntimeImageStorage(tmp_path / "image-cache")
    original_lock = storage.publication_lock
    contended = False

    def contend_once(archive_sha256: str):
        nonlocal contended
        if not contended:
            contended = True
            raise RuntimeImagePreparationError(
                "runtime_image.publication_contended",
                "another owner has the exact image publication lock",
                retryable=True,
                recovery_actions=("retry",),
            )
        return original_lock(archive_sha256)

    monkeypatch.setattr(storage, "publication_lock", contend_once)
    transport = Transport()
    service = _service(
        sessions,
        storage=storage,
        authority=lambda recipe_revision_id, **_: (recipe, _runtime()),
        transport=transport,
        clock=lambda: now[0],
    )
    operation = service.start(
        "publication-contention",
        actor="operator",
        request_id="publication-contention-request",
    )
    first_claim = service.claim_pending(owner_id="publication-worker")[0]
    service.run_claim(first_claim)

    assert service.get(operation.id).state == "queued"
    with sessions() as session:
        row = session.get(Job, operation.id)
        assert row is not None
        retry_state = row.payload["retry"]
        assert isinstance(retry_state, Mapping)
        assert retry_state["automatic_attempts"] >= 1
    now[0] += timedelta(seconds=6)
    retry = service.claim_pending(owner_id="publication-worker")[0]
    service.run_claim(retry)
    assert service.get(operation.id).state == "succeeded"
    assert transport.calls == 2
    engine.dispose()


@pytest.mark.usefixtures("damaged_json_rows")
def test_postgres_model_child_lock_contention_resumes_same_preparation(
    tmp_path: Path, postgres_engine
) -> None:
    """NOWAIT contention must defer the parent and reuse its exact child."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-source-build.json")
    now = [datetime.now(UTC)]
    with sessions.begin() as session:
        revision = _add_revision(session, "revision-pg-model-lock", recipe)
        _add_head(session, revision)

    child = SimpleNamespace(
        blockers=(),
        id="00000000-0000-4000-8000-000000000301",
        request_key="00000000-0000-4000-8000-000000000302",
        state="running",
        artifact_set_sha256="c" * 64,
        plan_digest="d" * 64,
        progress=progress_document(
            cache_progress(
                ModelCacheCounters.model_validate(
                    {
                        "phase": "downloading",
                        "downloaded_bytes": 40,
                        "expected_bytes": 100,
                        "completed_artifacts": 0,
                        "total_artifacts": 1,
                    }
                ),
                previous=None,
                now=now[0],
            )
        ),
        failure=None,
    )
    model_content_sha256 = recipe.models[0].model.content_sha256
    _persist_fake_model_cache_child(
        sessions,
        child=child,
        model_content_sha256=model_content_sha256,
        now=now[0],
    )

    class ModelCache:
        start_calls = 0

        def download_preview(self, *, recipe_revision_id: str) -> dict[str, object]:
            assert recipe_revision_id == revision.id
            return _download_preview(
                plan_digest=child.plan_digest,
                artifact_set_sha256=child.artifact_set_sha256,
                new_bytes=0,
            )

        def resolve_artifact_set(self, *, recipe_revision_id: str) -> SimpleNamespace:
            assert recipe_revision_id == revision.id
            return SimpleNamespace(
                blockers=(),
                digest=child.artifact_set_sha256,
                document=lambda: _manifest(
                    model_content_digests=[model_content_sha256]
                ),
            )

        def reusable_operation(self, **_: object) -> object:
            return child

        def start_download(self, **_: object) -> SimpleNamespace:
            self.start_calls += 1
            raise AssertionError("a lock retry must reuse the existing child")

        def get_operation(self, operation_id: str) -> SimpleNamespace:
            assert operation_id == child.id
            return child

    model_cache = ModelCache()
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        model_cache=model_cache,
        clock=lambda: now[0],
        claim_lease_seconds=10,
    )
    request_id = "10000000-0000-4000-8000-000000000301"
    queued = service.start(revision.id, actor="operator", request_id=request_id)
    claim = service.claim_pending(owner_id="lock-contention-worker")[0]

    # Earlier dependency waits must not turn row contention into minute backoff.
    with sessions.begin() as session:
        operation = session.get(Job, queued.id)
        assert operation is not None
        payload = AvailabilityJobPayload.model_validate_json(
            json.dumps(operation.payload)
        )
        operation.payload = serialize_json_value(
            payload.model_copy(
                update={
                    "retry": payload.retry.model_copy(update={"automatic_attempts": 10})
                }
            )
        )

    locker = sessions()
    try:
        locked_child = locker.scalar(
            select(ModelCacheOperation)
            .where(ModelCacheOperation.id == child.id)
            .with_for_update()
        )
        assert locked_child is not None
        service.run_claim(claim)
    finally:
        locker.rollback()
        locker.close()

    waiting = service.get(queued.id)
    assert waiting.state == "queued"
    assert waiting.failure is not None
    assert waiting.failure["retryable"] is True
    # A lock the download held for an instant is a named wait, never the raw
    # database error class and message.
    assert "OperationalError" not in str(waiting.failure["detail"])
    assert "could not obtain lock" not in str(waiting.failure["detail"])
    with sessions() as session:
        stored_waiting = session.get(Job, queued.id)
        assert stored_waiting is not None
        assert stored_waiting.request_id == request_id
        assert stored_waiting.state == "queued"
        retry_state = stored_waiting.payload["retry"]
        assert isinstance(retry_state, Mapping)
        # Dependency observations have the same request-owned retry deadline.
        assert retry_state["automatic_attempts"] >= 1
        retry_after_at = stored_waiting.next_action_at
        assert isinstance(retry_after_at, datetime)
        retry_at = retry_after_at.replace(tzinfo=UTC)
        assert 0 < (retry_at - now[0]).total_seconds() <= 5
        assert stored_waiting.payload.get("model_child") is None

    assert service.claim_pending(owner_id="too-early-worker") == ()
    with sessions() as session:
        still_waiting = session.get(Job, queued.id)
        assert still_waiting is not None
        retry_after = still_waiting.next_action_at
        assert isinstance(retry_after, datetime)
        assert retry_after.replace(tzinfo=UTC) == retry_at

    # Let the original bounded retry deadline elapse. The retry must select the
    # existing completed model operation under the same parent request.
    _persist_fake_model_cache_child(
        sessions,
        child=SimpleNamespace(
            id=child.id,
            request_key=child.request_key,
            state="succeeded",
            artifact_set_sha256=child.artifact_set_sha256,
            plan_digest=child.plan_digest,
            progress=progress_document(
                cache_progress(
                    ModelCacheCounters.model_validate(
                        {
                            "phase": "completed",
                            "downloaded_bytes": 100,
                            "expected_bytes": 100,
                            "completed_artifacts": 1,
                            "total_artifacts": 1,
                        }
                    ),
                    previous=None,
                    now=now[0],
                )
            ),
            failure=None,
        ),
        model_content_sha256=model_content_sha256,
        now=now[0],
    )
    child.state = "succeeded"
    child.progress = progress_document(
        cache_progress(
            ModelCacheCounters.model_validate(
                {
                    "phase": "completed",
                    "downloaded_bytes": 100,
                    "expected_bytes": 100,
                    "completed_artifacts": 1,
                    "total_artifacts": 1,
                }
            ),
            previous=None,
            now=now[0],
        )
    )
    now[0] = max(now[0] + timedelta(seconds=1), retry_at)
    retry = service.claim_pending(owner_id="lock-contention-recovery-worker")
    assert len(retry) == 1
    assert retry[0].operation_id == queued.id
    service.run_claim(retry[0])

    recovered = service.get(queued.id)
    assert recovered.state == "succeeded"
    assert recovered.result is not None
    recovered_child = require_mapping(
        recovered.result["model_child"], "recovered model child"
    )
    assert recovered_child["id"] == child.id
    assert model_cache.start_calls == 0
    with sessions() as session:
        stored_recovered = session.get(Job, queued.id)
        assert stored_recovered is not None
        assert stored_recovered.request_id == request_id
        assert stored_recovered.current_attempt == 2


def test_newer_preparation_intent_cancels_the_older_queued_preparation(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    successor = _successor(recipe, "Successor recipe revision")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_recipe_successors(
            session,
            older_id="revision-superseded",
            older=recipe,
            newer_id="revision-current",
            newer=successor,
        )
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (
            recipe if recipe_revision_id == "revision-superseded" else successor,
            _runtime(),
        ),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
        max_parallel=1,
    )
    older = service.start("revision-superseded", actor="operator", request_id="o" * 36)
    newer = service.start("revision-current", actor="operator", request_id="n" * 36)

    cancelled = service.get(older.id)
    assert cancelled.state == "cancelled"
    assert cancelled.result is None
    with sessions() as session:
        stored = session.get(Job, older.id)
        assert stored is not None
        assert stored.payload.get("claim_owner") is None
        assert stored.payload.get("claim_until") is None
        assert stored.result is None

    # The newer intent is untouched, and the cancelled preparation released its
    # single scheduler slot: the only claim available is the current revision.
    assert service.get(newer.id).state == "queued"
    claims = service.claim_pending(limit=1, owner_id="worker-a")
    assert [claim.operation_id for claim in claims] == [newer.id]
    assert service.resume_operations() == 1


def test_active_head_publication_keeps_exact_requested_preparation(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    successor = _successor(recipe, "Successor recipe revision")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_recipe_successors(
            session,
            older_id="revision-superseded",
            older=recipe,
            newer_id="revision-current",
            newer=successor,
        )
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (
            recipe if recipe_revision_id == "revision-superseded" else successor,
            _runtime(),
        ),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
        max_parallel=1,
    )
    older = service.start("revision-superseded", actor="operator", request_id="o" * 36)
    # A catalogue sync advances the active revision with no fresh download
    # request.  The dispatch boundary must refuse to start the older build.
    with sessions.begin() as session:
        _set_active_head(session, "revision-current")

    claims = service.claim_pending(limit=1, owner_id="worker-a")
    assert [claim.operation_id for claim in claims] == [older.id]
    service.run_claim(claims[0])
    completed = service.get(older.id)
    assert completed.state == LifecycleState.SUCCEEDED.value
    assert completed.recipe_revision_id == older.recipe_revision_id
    assert completed.artifact is not None


def test_running_preparation_for_an_older_revision_is_not_cancelled(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    successor = _successor(recipe, "Successor source-build revision")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_recipe_successors(
            session,
            older_id="revision-running",
            older=recipe,
            newer_id="revision-current",
            newer=successor,
        )
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (
            recipe if recipe_revision_id == "revision-running" else successor,
            _build_runtime(),
        ),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
        max_parallel=1,
        claim_lease_seconds=120,
    )
    running = service.start("revision-running", actor="operator", request_id="o" * 36)
    claim = service.claim_pending(limit=1, owner_id="worker-a")
    assert [item.operation_id for item in claim] == [running.id]
    with sessions.begin() as session:
        _set_active_head(session, "revision-current")

    # The older attempt is already building with a live lease.  Its shared build
    # inputs may still be reused by the active revision, so it is fail-closed
    # and must keep running.
    assert service.claim_pending(limit=1, owner_id="worker-b") == ()
    retained = service.get(running.id)
    assert retained.state == "running"
    assert retained.failure is None
    with sessions() as session:
        stored = session.get(Job, running.id)
        assert stored is not None
        assert stored.state == "running"
        assert stored.payload["claim_owner"] == "worker-a"


def test_resolved_preparation_keeps_its_consumer_when_same_input_intent_is_accepted(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-resolved", recipe)
    runtime = AvailabilityRuntime.model_validate(_runtime()).model_copy(
        update={"build_input_sha256": None, "input_intent_sha256": "a" * 64}
    )
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (
            recipe,
            runtime.model_dump(mode="json"),
        ),
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
    )
    first = service.start("revision-resolved", actor="operator", request_id="1" * 36)
    with sessions.begin() as session:
        row = session.get(Job, first.id)
        assert row is not None
        payload = service._payload(row)
        assert isinstance(payload, AvailabilityJobPayload)
        row.payload = payload.model_copy(
            update={
                "build_input_sha256": "b" * 64,
                "runtime": payload.runtime.model_copy(
                    update={"build_input_sha256": "b" * 64}
                ),
            }
        ).model_dump(mode="json", exclude_none=True)
    second = service.start("revision-resolved", actor="operator", request_id="2" * 36)
    assert first.id != second.id
    assert service.get(first.id).state == LifecycleState.QUEUED.value
    assert service.get(second.id).state == LifecycleState.QUEUED.value
    assert {claim.operation_id for claim in service.claim_pending(limit=2)} == {
        first.id,
        second.id,
    }


def test_request_replay_returns_original_before_metadata_refresh(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-replay", recipe)
    calls = 0

    def authority(
        recipe_revision_id: str, *, force: bool = False
    ) -> tuple[RecipeDefinition, dict[str, object]]:
        nonlocal calls
        calls += 1
        return recipe, _runtime()

    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=authority,
        transport=Transport(),
        clock=lambda: datetime.now(UTC),
    )
    first = service.start("revision-replay", actor="operator", request_id="7" * 36)
    replay = service.start("revision-replay", actor="operator", request_id="7" * 36)
    assert replay.id == first.id
    assert calls == 1


def test_same_work_identity_preserves_independent_authorization_operations(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-auth", recipe)
    transport = Transport()
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        clock=lambda: datetime.now(UTC),
    )
    first = service.start("revision-auth", actor="operator-a", request_id="8" * 36)
    second = service.start("revision-auth", actor="operator-b", request_id="9" * 36)
    assert second.id != first.id
    claims = service.claim_pending(limit=2, owner_id="worker-a")
    for claim in claims:
        service.run_claim(claim)
    assert service.get(first.id).state == LifecycleState.SUCCEEDED.value
    assert service.get(first.id).next_attempt_at is None
    assert service.get(second.id).state == LifecycleState.SUCCEEDED.value
    assert transport.calls == 1


@pytest.mark.usefixtures("damaged_json_rows")
def test_model_child_and_image_complete_through_one_sql_operation(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-model-image", recipe)
    now = datetime.now(UTC)

    child = SimpleNamespace(
        blockers=(),
        id="00000000-0000-4000-8000-000000000101",
        request_key="00000000-0000-4000-8000-000000000102",
        state="succeeded",
        artifact_set_sha256="c" * 64,
        plan_digest="d" * 64,
        progress=progress_document(
            cache_progress(
                ModelCacheCounters.model_validate(
                    {
                        "phase": "downloading",
                        "downloaded_bytes": 1024,
                        "expected_bytes": 1024,
                        "completed_artifacts": 0,
                        "total_artifacts": 1,
                    }
                ),
                previous=None,
                now=now,
            )
        ),
        failure=None,
    )
    _persist_fake_model_cache_child(
        sessions,
        child=child,
        model_content_sha256=next(
            model.model.content_sha256 for model in recipe.models
        ),
        now=now,
    )

    class ModelCache:
        def __init__(self) -> None:
            self.start_calls = 0

        def download_preview(self, *, recipe_revision_id: str) -> dict[str, object]:
            assert recipe_revision_id == "revision-model-image"
            return _download_preview(
                plan_digest="d" * 64, artifact_set_sha256="c" * 64, new_bytes=0
            )

        def resolve_artifact_set(self, *, recipe_revision_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                blockers=(),
                digest="c" * 64,
                document=lambda: _manifest(model_content_digests=["d" * 64]),
            )

        def reusable_operation(self, **_: object) -> object:
            return child if self.start_calls else None

        def start_download(self, **_: object) -> SimpleNamespace:
            self.start_calls += 1
            return child

        def get_operation(self, operation_id: str) -> SimpleNamespace:
            assert operation_id == child.id
            return child

    model_cache = ModelCache()
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=Transport(),
        model_cache=model_cache,
        clock=lambda: now,
    )
    queued = service.start(
        "revision-model-image", actor="operator", request_id="m" * 36
    )
    assert queued.model_child is None
    assert model_cache.start_calls == 0
    # Only the worker can issue children, after the parent is durable.
    assert service.run_pending() == 1
    for actor, key, force in [
        ("operator-2", "n" * 36, False),
        ("operator-3", "f" * 36, True),
    ]:
        later = service.start(
            "revision-model-image", actor=actor, request_id=key, force=force
        )
        assert later.model_child is None
        assert service.run_pending() == 1
        observed_child = service.get(later.id).model_child
        assert observed_child is not None
        assert observed_child["id"] == child.id
    assert model_cache.start_calls == 1
    completed = service.get(queued.id)
    assert completed.state == "succeeded"
    assert completed.result is not None
    assert (
        require_mapping(completed.result["model_child"], "model child")["id"]
        == child.id
    )
    from vonk_control.recipe_image_availability_api import _view_document

    response = _view_document(completed)
    assert response.result is not None
    assert response.result.model_content_digests == ["d" * 64]
    assert response.children[0].model_content_digests == ["d" * 64]


@pytest.mark.usefixtures("damaged_json_rows")
def test_model_and_image_children_advance_independently_and_reuse_image(
    tmp_path: Path,
) -> None:
    recipe = _recipe("recipe-source-build.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_revision(session, "revision-overlap", recipe)
    now = datetime.now(UTC)
    child = SimpleNamespace(
        blockers=(),
        id="00000000-0000-4000-8000-000000000201",
        request_key="00000000-0000-4000-8000-000000000202",
        state="running",
        artifact_set_sha256="c" * 64,
        plan_digest="d" * 64,
        progress=progress_document(
            cache_progress(
                ModelCacheCounters.model_validate(
                    {
                        "phase": "downloading",
                        "downloaded_bytes": 40,
                        "expected_bytes": 100,
                        "completed_artifacts": 0,
                        "total_artifacts": 1,
                    }
                ),
                previous=None,
                now=now,
            )
        ),
        failure=None,
    )
    _persist_fake_model_cache_child(
        sessions,
        child=child,
        model_content_sha256=next(
            model.model.content_sha256 for model in recipe.models
        ),
        now=now,
    )

    class ModelCache:
        def download_preview(self, **_: object) -> dict[str, object]:
            return _download_preview(
                plan_digest="d" * 64, artifact_set_sha256="c" * 64, new_bytes=0
            )

        def resolve_artifact_set(self, **_: object) -> SimpleNamespace:
            return SimpleNamespace(
                blockers=(),
                digest="c" * 64,
                document=lambda: _manifest(model_content_digests=["d" * 64]),
            )

        def reusable_operation(self, **_: object) -> None:
            return None

        def start_download(self, **_: object) -> SimpleNamespace:
            return child

        def get_operation(self, _operation_id: str) -> SimpleNamespace:
            return child

    transport = Transport()
    service = _service(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        model_cache=ModelCache(),
        clock=lambda: now,
    )
    queued = service.start("revision-overlap", actor="operator", request_id="q" * 36)
    assert service.run_pending() == 1
    partial = service.get(queued.id)
    assert partial.state == LifecycleState.BACKOFF
    assert partial.result is None
    assert transport.calls == 1
    assert partial.image_state == "succeeded"
    assert partial.image_failure is None
    assert partial.progress is not None
    assert partial.progress["completed_bytes"] == 40 + len(ARCHIVE)
    image_child = next(
        item
        for item in _view_document(partial).children
        if item.kind == "runtime-image"
    )
    assert image_child.state == "succeeded"
    assert image_child.progress.completed_bytes == len(ARCHIVE)
    assert (
        _progress_members(partial.progress["members"])[-1]["member_id"] == "model-cache"
    )
    assert isinstance(service._storage, FilesystemRuntimeImageStorage)
    remove_test_image(service._storage, ARCHIVE_SHA)
    child.state = "succeeded"
    with sessions.begin() as session:
        row = session.get(Job, queued.id)
        assert row is not None
        schedule_wait(row, datetime(2000, 1, 1, tzinfo=UTC))
    builds: list[bool] = []
    restarted = _service(
        sessions,
        storage=service._storage,
        builder=_builder(service._storage, calls=builds),
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        transport=transport,
        model_cache=ModelCache(),
        clock=lambda: datetime.now(UTC),
    )
    assert restarted.run_pending() == 1
    completed = restarted.get(queued.id)
    assert completed.state == "succeeded", completed.failure
    # The removed archive is built again rather than reused.
    assert builds == [False]
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        completed,
        end=lambda receipt: receipt,
        fresh=lambda _: restarted.start(
            "revision-overlap",
            actor="operator",
            request_id="prepare-after-rehydration",
        ),
    )
