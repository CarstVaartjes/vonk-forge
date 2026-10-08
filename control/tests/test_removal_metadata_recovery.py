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
        service.advance_removals(limit=1)
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
