"""Local metadata damage cannot veto verified content or authorize deletion."""

# ruff: noqa: F811 - imported cache fixture
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import select
from vonk_agent_protocol import ArtifactLifecycleCode, AssetAvailability, LifecycleState
from vonk_control import model_cache_states
from vonk_control.artifact_blob_store import ArtifactBlobStore
from vonk_control.artifact_jobs import ArtifactJobService
from vonk_control.artifact_lifecycle import ArtifactReferenceUnverified
from vonk_control.model_cache import CacheOperationView, removal_execution
from vonk_control.model_cache_contract import CacheManifest
from vonk_control.models import ArtifactJobBlob, ModelCacheSet, ModelCacheSetArtifact
from vonk_control.operation_contract import AvailabilityOperationFailure

from .non_blocking import assert_ended_without_blocking
from .test_artifact_jobs import running_artifact_service
from .test_model_cache import _download, cache  # noqa: F401
from .test_model_cache_lifecycle import _queue


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("damage", ["manifest", "membership", "surplus"])
def test_new_download_repairs_different_stored_content_and_membership(
    cache, tmp_path: Path, damage: str
):
    """Catches a damaged derived manifest refusing identical verified bytes forever."""
    service, sessions = cache
    accepted, artifact = _queue(service, tmp_path, str(uuid.uuid4()))
    service.run_pending()
    assert service.get_operation(accepted.id).state == LifecycleState.SUCCEEDED
    assert accepted.artifact_set_sha256 is not None
    with sessions.begin() as session:
        row = session.get(ModelCacheSet, accepted.artifact_set_sha256)
        assert row is not None
        manifest = CacheManifest.model_validate_json(json.dumps(row.manifest))
        damaged = manifest.model_copy(
            update={
                "artifacts": [
                    manifest.artifacts[0].model_copy(update={"sha256": "b" * 64})
                ]
            }
        )
        if damage == "manifest":
            row.manifest = damaged.model_dump(mode="json")
        member = session.scalar(
            select(ModelCacheSetArtifact).where(
                ModelCacheSetArtifact.artifact_set_sha256 == row.artifact_set_sha256
            )
        )
        assert member is not None
        if damage == "surplus":
            session.add(
                ModelCacheSetArtifact(
                    artifact_set_sha256=row.artifact_set_sha256,
                    artifact_key="surplus",
                    artifact_sha256="b" * 64,
                    path="stale-file",
                )
            )
        else:
            member.artifact_sha256 = "b" * 64
    # Successful reuse cannot depend on the original provider remaining present.
    Path(str(artifact["source"]).removeprefix("file://")).unlink()
    repaired = _download(
        service,
        [artifact],
        model_content_sha256="a" * 64,
        request_key=str(uuid.uuid4()),
    )
    assert repaired.state == LifecycleState.SUCCEEDED
    with sessions() as session:
        member = session.scalar(
            select(ModelCacheSetArtifact).where(
                ModelCacheSetArtifact.artifact_set_sha256
                == accepted.artifact_set_sha256
            )
        )
        assert (
            member is not None
            and member.artifact_sha256 == hashlib.sha256(b"x").hexdigest()
        )


@pytest.mark.usefixtures("damaged_json_rows")
def test_unreadable_manifest_projects_unknown_assets_and_keeps_bytes(
    cache, tmp_path: Path
):
    """Catches either refusing a read or inventing deletion-safe object lengths."""
    service, sessions = cache
    accepted, _artifact = _queue(service, tmp_path, str(uuid.uuid4()))
    service.run_pending()
    assert accepted.artifact_set_sha256 is not None
    with sessions() as session:
        scope = service._model_removal_scope_for_sets(
            session, (accepted.artifact_set_sha256,)
        )
    with sessions.begin() as session:
        row = session.get(ModelCacheSet, accepted.artifact_set_sha256)
        assert row is not None
        row.manifest = {}
    assets = service.removal_asset_status(scope)
    assert assets and all(
        asset.availability == AssetAvailability.UNKNOWN for asset in assets
    )
    assert all(asset.available_bytes is None for asset in assets)
    assert service._object_path(hashlib.sha256(b"x").hexdigest()).read_bytes() == b"x"
    fresh, _ = _queue(service, tmp_path, str(uuid.uuid4()))
    service.run_pending()
    assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED


def test_verified_blob_ingress_repairs_stale_sql_metadata(tmp_path: Path):
    """Catches interpreting stale SQL size/key as a cryptographic collision."""
    sessions, *_ = running_artifact_service(tmp_path)
    store = ArtifactBlobStore(tmp_path / "verified-blobs")
    data = b"verified content"
    digest = hashlib.sha256(data).hexdigest()
    stored = store.put_bytes(digest, data, maximum_bytes=len(data))
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            ArtifactJobBlob(
                sha256=digest, size_bytes=1, storage_key="stale", created_at=now
            )
        )
    with sessions.begin() as session:
        ArtifactJobService._put_blob_in_session(session, stored, now)
    with sessions() as session:
        row = session.get(ArtifactJobBlob, digest)
        assert row is not None and row.size_bytes == len(data)
        assert row.storage_key == stored.storage_key
    assert stored.path.read_bytes() == data


def test_unavailable_scan_ends_with_bytes_retained_and_fresh_download_admitted(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Catches indefinite retry, unsafe unlink, or a terminal removal stranding gates."""
    service, sessions = cache
    accepted, _ = _queue(service, tmp_path, str(uuid.uuid4()))
    service.run_pending()
    assert accepted.artifact_set_sha256 is not None
    with sessions.begin() as session:
        removal = service.accept_removal_for_sets_in_session(
            session,
            actor="test",
            request_key=str(uuid.uuid4()),
            selector="recipe-child",
            selected_sets=(accepted.artifact_set_sha256,),
        )

    def unavailable(*_args, **_kwargs):
        raise ArtifactReferenceUnverified(
            ArtifactLifecycleCode.REFERENCE_SCAN_FAILED, "observation unavailable"
        )

    monkeypatch.setattr(removal_execution, "model_set_reference_reasons", unavailable)
    now = [datetime.now(UTC)]
    service._clock = lambda: now[0]
    for _ in range(8):
        service.advance_removals(limit=1)
        now[0] += timedelta(minutes=1)
        if service.get_operation(removal.id).state == LifecycleState.FAILED:
            break
    ended = service.get_operation(removal.id)
    assert ended.state not in model_cache_states.LIVE
    assert ended.state != LifecycleState.SUCCEEDED
    assert service._object_path(hashlib.sha256(b"x").hexdigest()).read_bytes() == b"x"

    def validate_failure(receipt: CacheOperationView) -> None:
        AvailabilityOperationFailure.model_validate(receipt.failure)

    _, fresh = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        ended,
        end=lambda receipt: receipt,
        assert_reason=validate_failure,
        fresh=lambda _: _queue(service, tmp_path, str(uuid.uuid4()))[0],
    )
    service.run_pending()
    assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED


@pytest.mark.parametrize("restored", [True, False])
def test_recipe_scan_unknown_is_accepted_and_never_blocks_fresh_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restored: bool
):
    """Catches refusing removal before durable intent or unlinking on an unknown scan."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_control.models import Base
    from vonk_control.recipe_image_availability import (
        removal_execution as image_execution,
    )
    from vonk_control.recipe_image_availability import removal_review as image_review
    from vonk_control.recipe_image_availability_view_contract import (
        RecipeCacheRemovalStatus,
        RecipeImageAvailabilityView,
    )
    from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

    from .recipe_removal_review_support import remove_after_review
    from .runtime_image_fixtures import place_test_image
    from .test_recipe_image_availability import (
        ARCHIVE,
        ARCHIVE_SHA,
        _add_head,
        _add_revision,
        _recipe,
        _reference_receipt,
        _runtime,
        _service,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    recipe = _recipe("recipe-source-build.json")
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "scan-recovery", recipe))
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")
    place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
    receipt = _reference_receipt()
    receipt_path = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    receipt_path.write_text(receipt.model_dump_json())
    now = [datetime.now(UTC)]
    service = _service(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now[0],
    )

    def unknown(*_args, **_kwargs):
        raise ArtifactReferenceUnverified(
            ArtifactLifecycleCode.REFERENCE_SCAN_FAILED, "observation unavailable"
        )

    with monkeypatch.context() as fault:
        fault.setattr(image_review, "runtime_image_reference_findings", unknown)
        fault.setattr(image_execution, "runtime_image_reference_reasons", unknown)
        key = str(uuid.uuid4())
        accepted = remove_after_review(
            service, recipe.identity.slug, actor="operator", request_id=key
        )
        assert accepted["operation_id"]
        for _ in range(3):
            service.advance_removals(limit=1)
            waiting = service.get_operator_request(key, actor="operator")
            if (
                isinstance(waiting, RecipeCacheRemovalStatus)
                and waiting.state == LifecycleState.BACKOFF
            ):
                break
        waiting = service.get_operator_request(key, actor="operator")
        assert isinstance(waiting, RecipeCacheRemovalStatus)
        assert waiting.state == LifecycleState.BACKOFF
        assert receipt_path.exists()
        assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()
        if not restored:
            for _ in range(8):
                now[0] += timedelta(minutes=1)
                service.advance_removals(limit=1)
    now[0] += timedelta(minutes=1)
    for _ in range(4):
        service.advance_removals(limit=1)
    finished = service.get_operator_request(key, actor="operator")
    assert isinstance(finished, RecipeCacheRemovalStatus)
    assert finished.operation_id == waiting.operation_id
    if restored:
        assert finished.state == LifecycleState.SUCCEEDED
        assert not receipt_path.exists()
    else:
        assert finished.state not in model_cache_states.LIVE
        assert finished.state != LifecycleState.SUCCEEDED
        assert receipt_path.exists()
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        cast(RecipeCacheRemovalStatus | RecipeImageAvailabilityView, finished),
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "scan-recovery",
            actor="operator",
            request_id=str(uuid.uuid4()),
        ),
    )
    engine.dispose()


def test_logical_removal_discovers_missing_index_only_after_admission(
    cache, tmp_path: Path
):
    """Catches an absent set index producing a successful empty removal."""
    from .test_model_cache_lifecycle import MODEL

    service, sessions = cache
    cached, artifact = _queue(service, tmp_path, str(uuid.uuid4()))
    service.run_pending()
    assert cached.artifact_set_sha256 is not None
    object_path = service._object_path(hashlib.sha256(b"x").hexdigest())
    with sessions.begin() as session:
        row = session.get(ModelCacheSet, cached.artifact_set_sha256)
        assert row is not None
        session.delete(row)
    request = service.remove_model_selector(
        MODEL, actor="test", request_key=str(uuid.uuid4())
    )
    assert request.state in model_cache_states.LIVE
    assert object_path.read_bytes() == b"x"
    for _ in range(8):
        service.advance_removals(limit=1)
    assert service.get_operation(request.id).state == LifecycleState.SUCCEEDED
    assert not object_path.exists()
    _, fresh = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        request,
        end=lambda receipt: service.get_operation(receipt.id),
        fresh=lambda _: _download(
            service,
            [artifact],
            model_content_sha256=MODEL,
            request_key=str(uuid.uuid4()),
        ),
    )
    assert fresh.state == LifecycleState.SUCCEEDED


def test_newer_preparation_wins_before_pending_scope_acquires_gates(
    cache, tmp_path: Path
):
    """Catches pending discovery deleting bytes a newer request already reused."""
    from .test_model_cache_lifecycle import MODEL, _clock_at

    service, sessions = cache
    now = _clock_at(service)
    cached, artifact = _queue(service, tmp_path, str(uuid.uuid4()))
    service.run_pending()
    assert cached.artifact_set_sha256 is not None
    with sessions.begin() as session:
        row = session.get(ModelCacheSet, cached.artifact_set_sha256)
        assert row is not None
        session.delete(row)
    now[0] += timedelta(seconds=1)
    removal = service.remove_model_selector(
        MODEL, actor="test", request_key=str(uuid.uuid4())
    )
    Path(str(artifact["source"]).removeprefix("file://")).unlink()
    now[0] += timedelta(seconds=1)
    reused = _download(
        service,
        [artifact],
        model_content_sha256=MODEL,
        request_key=str(uuid.uuid4()),
    )
    assert reused.state == LifecycleState.SUCCEEDED
    service.advance_removals(limit=1)
    assert service.get_operation(removal.id).state == LifecycleState.CANCELLED
    assert service._object_path(hashlib.sha256(b"x").hexdigest()).read_bytes() == b"x"
    _, fresh = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        removal,
        end=lambda receipt: service.get_operation(receipt.id),
        fresh=lambda _: _download(
            service,
            [artifact],
            model_content_sha256=MODEL,
            request_key=str(uuid.uuid4()),
        ),
    )
    assert fresh.state == LifecycleState.SUCCEEDED


def test_explicit_empty_scope_cannot_expand_to_logical_content(cache, tmp_path: Path):
    """Catches scope reconstruction broadening an explicitly empty effect."""
    from .test_model_cache_lifecycle import MODEL

    service, sessions = cache
    cached, artifact = _queue(service, tmp_path, str(uuid.uuid4()))
    service.run_pending()
    object_path = service._object_path(hashlib.sha256(b"x").hexdigest())
    with sessions.begin() as session:
        request = service._accept_model_removal(
            session,
            actor="test",
            request_key=str(uuid.uuid4()),
            selector=MODEL,
            model_content_sha256=MODEL,
            selected_sets=(),
        )
        operation_id = request.id
    for _ in range(8):
        service.advance_removals(limit=1)
    assert service.get_operation(operation_id).state == LifecycleState.SUCCEEDED
    assert object_path.read_bytes() == b"x"
    assert service.get_operation(cached.id).state == LifecycleState.SUCCEEDED
    _, fresh = assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        service.get_operation(operation_id),
        end=lambda receipt: receipt,
        fresh=lambda _: _download(
            service,
            [artifact],
            model_content_sha256=MODEL,
            request_key=str(uuid.uuid4()),
        ),
    )
    assert fresh.state == LifecycleState.SUCCEEDED


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("damage", ["missing-set", "manifest", "membership"])
def test_damaged_exact_scope_is_admitted_and_reconstructed(
    cache, tmp_path: Path, damage: str
):
    """Catches pre-owner scope rejection and destructive use of damaged membership."""
    service, sessions = cache
    accepted, artifact = _queue(service, tmp_path, str(uuid.uuid4()))
    service.run_pending()
    digest = accepted.artifact_set_sha256
    assert digest is not None
    object_path = service._object_path(hashlib.sha256(b"x").hexdigest())
    Path(str(artifact["source"]).removeprefix("file://")).unlink()
    with sessions.begin() as session:
        row = session.get(ModelCacheSet, digest)
        assert row is not None
        if damage == "missing-set":
            session.delete(row)
        elif damage == "manifest":
            row.manifest = {}
        else:
            member = session.scalar(
                select(ModelCacheSetArtifact).where(
                    ModelCacheSetArtifact.artifact_set_sha256 == digest
                )
            )
            assert member is not None
            member.artifact_sha256 = "b" * 64
    with sessions.begin() as session:
        removal = service.accept_removal_for_sets_in_session(
            session,
            actor="test",
            request_key=str(uuid.uuid4()),
            selector="exact-content",
            selected_sets=(digest,),
        )
    assert object_path.read_bytes() == b"x"
    for _ in range(8):
        service.advance_removals(limit=1)
        if service.get_operation(removal.id).state not in model_cache_states.LIVE:
            break
    assert service.get_operation(removal.id).state == LifecycleState.SUCCEEDED
    assert not object_path.exists()
    # A new exact removal has its own owner, independently of the ended request.
    with sessions.begin() as session:
        fresh = service.accept_removal_for_sets_in_session(
            session,
            actor="test",
            request_key=str(uuid.uuid4()),
            selector="exact-content",
            selected_sets=(digest,),
        )
    assert fresh.id != removal.id


@pytest.mark.usefixtures("damaged_json_rows")
def test_scope_observation_exhausts_without_inventing_objects(cache, tmp_path: Path):
    """Catches damaged scope deleting guessed objects or poisoning a new request."""
    from vonk_control.models import ModelCacheOperation

    service, sessions = cache
    accepted, _ = _queue(service, tmp_path, str(uuid.uuid4()))
    service.run_pending()
    digest = accepted.artifact_set_sha256
    assert digest is not None
    with sessions.begin() as session:
        row = session.get(ModelCacheSet, digest)
        producer = session.get(ModelCacheOperation, accepted.id)
        assert row is not None and producer is not None
        row.manifest = {}
        producer.payload = {}
        removal = service.accept_removal_for_sets_in_session(
            session,
            actor="test",
            request_key=str(uuid.uuid4()),
            selector="exact-content",
            selected_sets=(digest,),
        )
    now = [datetime.now(UTC)]
    service._clock = lambda: now[0]
    for _ in range(8):
        service.advance_removals(limit=1)
        now[0] += timedelta(minutes=1)
        if service.get_operation(removal.id).state not in model_cache_states.LIVE:
            break
    assert service.get_operation(removal.id).state == LifecycleState.FAILED
    assert service._object_path(hashlib.sha256(b"x").hexdigest()).read_bytes() == b"x"
    with sessions.begin() as session:
        fresh = service.accept_removal_for_sets_in_session(
            session,
            actor="test",
            request_key=str(uuid.uuid4()),
            selector="exact-content",
            selected_sets=(digest,),
        )
    assert fresh.state in model_cache_states.LIVE and fresh.id != removal.id


@pytest.mark.parametrize("damage", ["missing", "unreadable", "size"])
@pytest.mark.parametrize("repair", [True, False])
def test_recipe_receipt_observation_is_request_owned(
    tmp_path: Path, damage: str, repair: bool
):
    """Catches receipt admission veto, unverified deletion and permanently owned bytes."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_control.models import ArtifactLifecycleGate, Base
    from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

    from .runtime_image_fixtures import place_test_image
    from .test_recipe_image_availability import (
        ARCHIVE,
        ARCHIVE_SHA,
        _add_head,
        _add_revision,
        _recipe,
        _reference_receipt,
        _runtime,
        _service,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    recipe = _recipe("recipe-source-build.json")
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "receipt-observation", recipe))
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")
    place_test_image(storage, ARCHIVE_SHA, len(ARCHIVE))
    receipt_path = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    valid = _reference_receipt().model_dump_json()
    receipt_path.write_text(valid)
    if damage == "missing":
        receipt_path.unlink()
    elif damage == "unreadable":
        receipt_path.write_text("{")
    else:
        receipt_path.write_text(
            _reference_receipt()
            .model_copy(update={"image_bytes": len(ARCHIVE) + 1})
            .model_dump_json()
        )
    now = [datetime.now(UTC)]
    service = _service(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now[0],
    )
    request_key = str(uuid.uuid4())
    accepted = service.remove_selector(
        recipe.identity.slug, actor="operator", request_id=request_key
    )
    assert accepted.operation_id
    service.advance_removals(limit=1)
    assert (storage.layout.root / "blobs" / "sha256" / ARCHIVE_SHA).exists()
    if repair:
        receipt_path.write_text(valid)
    for _ in range(8):
        now[0] += timedelta(minutes=1)
        service.advance_removals(limit=1)
    ended = service.get_operator_request(request_key, actor="operator")
    assert ended.state == (
        LifecycleState.SUCCEEDED if repair else LifecycleState.FAILED
    )
    with sessions() as session:
        assert (
            session.scalar(
                select(ArtifactLifecycleGate).where(
                    ArtifactLifecycleGate.removal_owner_id == accepted.operation_id
                )
            )
            is None
        )
    if not repair:
        assert (storage.layout.root / "blobs" / "sha256" / ARCHIVE_SHA).exists()
    assert_ended_without_blocking(
        SimpleNamespace(sessions=sessions),
        ended,
        end=lambda receipt: receipt,
        fresh=lambda _: service.start(
            "receipt-observation", actor="operator", request_id=str(uuid.uuid4())
        ),
    )


@pytest.mark.parametrize("owner_damage", ["missing", "payload", "fence", "coverage"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_exact_scope_owner_repair_is_independent_of_inventory_cursor(
    cache, tmp_path: Path, owner_damage: str
):
    """Catches repairing only the first global batch and retaining the exact old fence."""
    from vonk_control.models import ArtifactLifecycleGate, ModelCacheOperation

    service, sessions = cache
    accepted, _ = _queue(service, tmp_path, str(uuid.uuid4()))
    service.run_pending()
    digest = accepted.artifact_set_sha256
    assert digest is not None
    with sessions.begin() as session:
        old = service.accept_removal_for_sets_in_session(
            session,
            actor="test",
            request_key=str(uuid.uuid4()),
            selector="exact-content",
            selected_sets=(digest,),
        )
        row = session.get(ModelCacheOperation, old.id)
        assert row is not None
        fence = str(uuid.uuid4())
        if owner_damage == "payload":
            row.payload = {}
        elif owner_damage == "missing":
            session.delete(row)
        elif owner_damage == "coverage":
            row.payload = dict(row.payload) | {"selected": []}
        # More stale gates than the global inventory's batch must not make this
        # request rely on its global cursor reaching the target.
        for index in range(70):
            session.add(
                ArtifactLifecycleGate(
                    artifact_kind="model-object",
                    artifact_sha256=f"{index:064x}",
                    removal_owner_kind="model-cache-operation",
                    removal_owner_id=str(uuid.uuid4()),
                    removal_fence=str(uuid.uuid4()),
                    updated_at=datetime.now(UTC),
                )
            )
        gate = session.get(ArtifactLifecycleGate, ("model-set", digest))
        if gate is None:
            gate = ArtifactLifecycleGate(
                artifact_kind="model-set",
                artifact_sha256=digest,
                updated_at=datetime.now(UTC),
            )
            session.add(gate)
        gate.removal_owner_kind = "model-cache-operation"
        gate.removal_owner_id = old.id
        gate.removal_fence = fence
        fresh = service.accept_removal_for_sets_in_session(
            session,
            actor="test",
            request_key=str(uuid.uuid4()),
            selector="exact-content",
            selected_sets=(digest,),
        )
    now = [datetime.now(UTC)]
    service._clock = lambda: now[0]
    for _ in range(8):
        service.advance_removals(limit=2)
        now[0] += timedelta(seconds=20)
        if service.get_operation(fresh.id).state not in model_cache_states.LIVE:
            break
    assert service.get_operation(fresh.id).state == LifecycleState.SUCCEEDED
    with sessions() as session:
        exact = session.get(ArtifactLifecycleGate, ("model-set", digest))
        assert exact is None or exact.removal_owner_id is None
        if owner_damage in {"payload", "coverage"}:
            damaged = session.get(ModelCacheOperation, old.id)
            assert damaged is not None and damaged.state not in model_cache_states.LIVE
    next_request, _ = _queue(service, tmp_path, str(uuid.uuid4()))
    service.run_pending()
    assert service.get_operation(next_request.id).state == LifecycleState.SUCCEEDED
