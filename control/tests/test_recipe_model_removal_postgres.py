"""Connected recipe removal waits for its exact real model-cache child."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

from sqlalchemy import Table, func, select
from sqlalchemy.orm import sessionmaker
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_contract import (
    ModelCacheRemovalPayload,
    ModelCacheRemovalResult,
    parse_model_cache_payload,
)
from vonk_control.models import (
    Base,
    FleetProfile,
    FleetProfileApplication,
    Job,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RuntimeImageAuthorization,
)
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.recipe_image_removal_contract import (
    RecipeCacheRemovalOwner,
    RecipeCacheRemovalResult,
)
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import RecipeDefinition, content_sha256

from .recipe_removal_review_support import remove_after_review
from .test_model_removal_reference_lifecycle import (
    _one_model,
    _register_model,
    _removal_service,
    _seed_model,
)
from .test_recipe_image_availability import (
    ARCHIVE,
    ARCHIVE_SHA,
    _add_head,
    _add_revision,
    _recipe,
    _reference_receipt,
    _runtime,
)


def _recipe_using_model(
    model_digest: str, publisher: str, slug: str
) -> RecipeDefinition:
    document = json.loads(_recipe("recipe-image.json").model_dump_json())
    assert isinstance(document, dict)
    models = document.get("models")
    assert isinstance(models, list) and len(models) == 1
    model_selection = models[0]
    assert isinstance(model_selection, dict)
    model_reference = model_selection.get("model")
    assert isinstance(model_reference, dict)
    model_reference.update(
        content_sha256=model_digest,
        publisher=publisher,
        slug=slug,
    )
    return RecipeDefinition.model_validate(document)


def test_postgres_recipe_removal_with_model_resumes_exact_child_after_service_restart(
    postgres_engine, tmp_path: Path
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    model_service, _sessions = _removal_service(postgres_engine, tmp_path / "models")
    model_root = model_service.root
    model_bytes = b"abc"

    selected_model = _one_model(tmp_path, "synthetic-tiny-fp16", model_bytes)
    selected_selector = _register_model(sessions, selected_model)
    model_digest, _artifact, selected_set = _seed_model(
        model_service,
        tmp_path,
        selected_model,
        str(uuid4()),
    )
    sibling_model = _one_model(tmp_path, "synthetic-tiny-shared-sibling", model_bytes)
    _register_model(sessions, sibling_model)
    sibling_digest, _sibling_artifact, sibling_set = _seed_model(
        model_service,
        tmp_path,
        sibling_model,
        str(uuid4()),
    )
    assert selected_selector == "vonk-forge/synthetic-tiny-fp16"
    assert model_digest == content_sha256(selected_model)
    assert sibling_digest == content_sha256(sibling_model)
    assert selected_set != sibling_set

    shared_object_digest = hashlib.sha256(model_bytes).hexdigest()
    shared_object = model_service._object_path(shared_object_digest)
    assert shared_object.read_bytes() == model_bytes
    with sessions() as session:
        memberships = tuple(
            session.scalars(
                select(ModelCacheSetArtifact.artifact_set_sha256).where(
                    ModelCacheSetArtifact.artifact_sha256 == shared_object_digest
                )
            )
        )
    assert set(memberships) == {selected_set, sibling_set}

    recipe = _recipe_using_model(
        model_digest,
        selected_model.identity.publisher,
        selected_model.identity.slug,
    )
    now = [datetime.now(UTC)]
    revision_id = "rev-model-remove-001"
    receipt = _reference_receipt()
    with sessions.begin() as session:
        revision = _add_revision(session, revision_id, recipe)
        _add_head(session, revision)
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=revision.id,
                source="published",
                original_content_digest=revision.content_digest,
                effective_execution_key=revision.execution_key,
                registry_manifest_digest=receipt.registry_manifest_digest,
                platform_manifest_digest=receipt.platform_manifest_digest,
                local_image_config_id=receipt.local_image_config_id,
                oci_archive_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
                build_id=None,
                authorized_at=now[0],
                state="authorized",
            )
        )

    image_root = tmp_path / "runtime-images"
    image_storage = FilesystemRuntimeImageStorage(image_root)
    archive = image_storage.root / ARCHIVE_SHA
    receipt_path = image_storage.root / f"{ARCHIVE_SHA}.receipt.json"
    archive.write_bytes(ARCHIVE)
    receipt_path.write_text(
        json.dumps(receipt.model_dump(mode="json")), encoding="utf-8"
    )

    def new_recipe_service(cache: ModelCacheService) -> RecipeImageAvailabilityService:
        return RecipeImageAvailabilityService(
            sessions,
            storage=FilesystemRuntimeImageStorage(image_root),
            authority=lambda *_args, **_kwargs: (recipe, _runtime()),
            model_cache=cache,
            clock=lambda: now[0],
        )

    recipe_service = new_recipe_service(model_service)
    request_id = str(uuid4())
    review = recipe_service.review_removal(recipe.identity.slug, with_model=True)
    retained_object = next(
        asset
        for asset in review.assets
        if asset.kind == "model-object" and asset.sha256 == shared_object_digest
    )
    assert retained_object.disposition == "retain-shared"
    assert retained_object.availability == "verified"
    assert any(
        finding.classification == "saved-reference"
        and finding.asset_kind == "model-object"
        and finding.asset_sha256 == shared_object_digest
        and finding.owner_kind == "model-cache-set-membership"
        and finding.owner_id == sibling_set
        for finding in review.references
    )
    accepted = remove_after_review(
        recipe_service,
        recipe.identity.slug,
        actor="operator",
        request_id=request_id,
        with_model=True,
        review_digest=review.review_digest,
    )
    assert accepted["state"] == "queued"
    parent_id = str(accepted["operation_id"])

    with sessions() as session:
        parent_row = session.get(Job, parent_id)
        assert parent_row is not None and parent_row.state == "queued"
        parent_owner = RecipeCacheRemovalOwner.model_validate_json(
            json.dumps(parent_row.payload)
        )
        assert parent_owner.plan.intent.with_model is True
        assert parent_owner.plan.image_archives == [ARCHIVE_SHA]
        assert len(parent_owner.plan.model_children) == 1
        child = parent_owner.plan.model_children[0]
        assert child.selected_sets == [selected_set]
        child_row = session.get(ModelCacheOperation, child.operation_id)
        assert child_row is not None
        assert child_row.id == child.operation_id
        assert child_row.kind == "remove"
        assert child_row.request_key == child.request_key
        assert child_row.plan_digest == child.plan_digest
        child_payload = parse_model_cache_payload("remove", child_row.payload)
        assert isinstance(child_payload, ModelCacheRemovalPayload)
        assert child_payload.selected == child.selected_sets
        assert child_payload.delete_objects == []

    child_id = child.operation_id
    child_request_key = child.request_key
    child_plan_digest = child.plan_digest
    assert accepted["model_removals"] == [child_id]

    # The parent removes its published image first, then waits on the child
    # owner without claiming the child's model effects have completed.
    assert recipe_service.advance_removals(limit=1) == 1
    assert not archive.exists()
    assert not receipt_path.exists()
    assert recipe_service.advance_removals(limit=1) == 1
    pending = recipe_service.get_operator_request(request_id, actor="operator")
    assert isinstance(pending, dict)
    assert pending["operation_id"] == parent_id
    assert pending["state"] == "partial"
    with sessions() as session:
        pending_parent = session.get(Job, parent_id)
        assert pending_parent is not None and pending_parent.result is None
        pending_owner = RecipeCacheRemovalOwner.model_validate_json(
            json.dumps(pending_parent.payload)
        )
        assert pending_owner.checkpoint.image_index == 1
        assert pending_owner.checkpoint.image_reclaimed_bytes == len(ARCHIVE)
        assert pending_owner.checkpoint.model_index == 0
        assert pending_owner.checkpoint.model_reclaimed_bytes == 0
        assert pending_owner.checkpoint.failure is not None
        assert (
            pending_owner.checkpoint.failure.code == "model_cache.removal_child_pending"
        )
        pending_child = session.get(ModelCacheOperation, child_id)
        assert pending_child is not None and pending_child.state == "queued"
        assert pending_child.request_key == child_request_key
        assert pending_child.plan_digest == child_plan_digest
    assert shared_object.read_bytes() == model_bytes

    # Recreate both services against the same PostgreSQL authority and managed
    # directories. A replay resumes the persisted child; it never creates a
    # replacement request or derives a new child identity.
    model_service.close()
    restarted_cache = ModelCacheService(
        sessions,
        model_root,
        reserve_bytes=0,
        fixture_sources=True,
        clock=lambda: now[0],
    )
    restarted_recipe_service = new_recipe_service(restarted_cache)
    replay = remove_after_review(
        restarted_recipe_service,
        recipe.identity.slug,
        actor="operator",
        request_id=request_id,
        with_model=True,
        review_digest=str(accepted["review_digest"]),
    )
    assert isinstance(replay, dict)
    assert replay["operation_id"] == parent_id
    assert replay["model_removals"] == [child_id]
    assert replay["state"] == "partial"
    with sessions() as session:
        replayed_parent = session.get(Job, parent_id)
        assert replayed_parent is not None
        replayed_owner = RecipeCacheRemovalOwner.model_validate_json(
            json.dumps(replayed_parent.payload)
        )
        replayed_child = replayed_owner.plan.model_children[0]
        assert replayed_child.operation_id == child_id
        assert replayed_child.request_key == child_request_key
        assert replayed_child.plan_digest == child_plan_digest
        assert (
            session.scalar(
                select(func.count())
                .select_from(ModelCacheOperation)
                .where(ModelCacheOperation.request_key == child_request_key)
            )
            == 1
        )

    child_view = restarted_cache.get_operation(child_id)
    for _ in range(8):
        if child_view.state == "succeeded":
            break
        assert restarted_cache.advance_removals(limit=1) == 1
        child_view = restarted_cache.get_operation(child_id)
    assert child_view.state == "succeeded"
    assert child_view.id == child_id
    assert child_view.request_key == child_request_key
    assert child_view.plan_digest == child_plan_digest
    assert isinstance(child_view.result, ModelCacheRemovalResult)
    assert child_view.result.removed_entries == [selected_set]
    assert child_view.result.reclaimed_bytes == 0
    assert shared_object.read_bytes() == model_bytes
    with sessions() as session:
        assert session.get(ModelCacheSet, selected_set) is None
        assert session.get(ModelCacheSet, sibling_set) is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(ModelCacheSetArtifact)
                .where(ModelCacheSetArtifact.artifact_sha256 == shared_object_digest)
            )
            == 1
        )

    now[0] += timedelta(seconds=10)
    for _ in range(3):
        restarted_recipe_service.advance_removals(limit=1)
        final = restarted_recipe_service.get_operator_request(
            request_id, actor="operator"
        )
        assert isinstance(final, dict)
        if final["state"] == "succeeded":
            break
    assert final["operation_id"] == parent_id
    assert final["state"] == "succeeded"
    assert final["model_removals"] == [child_id]
    assert final["reclaimed_bytes"] == len(ARCHIVE) + child_view.result.reclaimed_bytes
    with sessions() as session:
        final_parent = session.get(Job, parent_id)
        assert final_parent is not None and final_parent.result is not None
        final_owner = RecipeCacheRemovalOwner.model_validate_json(
            json.dumps(final_parent.payload)
        )
        final_result = RecipeCacheRemovalResult.model_validate_json(
            json.dumps(final_parent.result)
        )
        assert final_owner.plan.model_children[0].operation_id == child_id
        assert final_owner.plan.model_children[0].request_key == child_request_key
        assert final_owner.plan.model_children[0].plan_digest == child_plan_digest
        assert final_owner.checkpoint.image_index == len(
            final_owner.plan.image_archives
        )
        assert final_owner.checkpoint.model_index == len(
            final_owner.plan.model_children
        )
        assert final_owner.checkpoint.image_reclaimed_bytes == len(ARCHIVE)
        assert (
            final_owner.checkpoint.model_reclaimed_bytes
            == child_view.result.reclaimed_bytes
        )
        assert final_result.reclaimed_bytes == final["reclaimed_bytes"]
        assert (
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.request_id == request_id)
            )
            == 1
        )
    assert not archive.exists()
    assert not receipt_path.exists()
    assert shared_object.read_bytes() == model_bytes
    restarted_cache.close()


def test_postgres_recipe_review_recovers_from_failed_profile_scan(
    postgres_engine, tmp_path: Path
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-image.json")
    revision_id = "rev-review-savepoint-001"
    receipt = _reference_receipt()
    with sessions.begin() as session:
        revision = _add_revision(session, revision_id, recipe)
        _add_head(session, revision)
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=revision.id,
                source="published",
                original_content_digest=revision.content_digest,
                effective_execution_key=revision.execution_key,
                registry_manifest_digest=receipt.registry_manifest_digest,
                platform_manifest_digest=receipt.platform_manifest_digest,
                local_image_config_id=receipt.local_image_config_id,
                oci_archive_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
                build_id=None,
                authorized_at=datetime.now(UTC),
                state="authorized",
            )
        )

    image_root = tmp_path / "review-runtime-images"
    storage = FilesystemRuntimeImageStorage(image_root)
    archive = storage.root / ARCHIVE_SHA
    receipt_path = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    archive.write_bytes(ARCHIVE)
    receipt_path.write_text(json.dumps(receipt.model_dump(mode="json")))
    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
    )

    # The real profile owner query fails with PostgreSQL undefined_table. The
    # review must roll back that scanner savepoint before it reads lifecycle
    # gates and returns a fail-closed blocker.
    cast(Table, FleetProfileApplication.__table__).drop(postgres_engine)
    cast(Table, FleetProfile.__table__).drop(postgres_engine)
    try:
        refused = service.review_removal(recipe.identity.slug, with_model=False)
        assert any(
            blocker.code == "artifact.reference_scan_failed"
            for blocker in refused.blockers
        )
        assert archive.read_bytes() == ARCHIVE

        Base.metadata.create_all(postgres_engine)
        recovered = service.review_removal(recipe.identity.slug, with_model=False)
        assert all(
            blocker.code != "artifact.reference_scan_failed"
            for blocker in recovered.blockers
        )
    finally:
        Base.metadata.create_all(postgres_engine)
