"""Cache removal must wait for cancellation settlement without harming peers."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.model_cache import ModelCacheConflict, ModelCacheService
from vonk_control.models import (
    Base,
    CatalogDocument,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    User,
)
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from .test_model_cache import _artifact, _canonical_model, _download
from .test_model_removal_reference_lifecycle import _register_model
from .test_recipe_image_availability import (
    Transport,
    _add_revision,
    _recipe,
    _runtime,
)
from .test_recipe_model_child_cancel_integration import _hold_model_worker


def _cache_adapter(
    cache: ModelCacheService,
    *,
    recipe_revision_id: str,
    model_digest: str,
    artifacts: list[dict[str, object]],
) -> Any:
    class Adapter:
        def download_preview(self, *, recipe_revision_id: str):
            assert recipe_revision_id == self.recipe_revision_id
            return cache.download_preview(
                model_content_sha256=model_digest,
                artifacts=artifacts,
            )

        def resolve_artifact_set(self, *, recipe_revision_id: str):
            assert recipe_revision_id == self.recipe_revision_id
            return cache.resolve_artifact_set(
                model_content_sha256=model_digest,
                artifacts=artifacts,
            )

        def list_operations(self, *, limit: int = 100):
            return cache.list_operations(limit=limit)

        def start_download(self, **kwargs: Any):
            kwargs.pop("recipe_revision_id", None)
            return cache.start_download(
                model_content_sha256=model_digest,
                artifacts=artifacts,
                **kwargs,
            )

        def get_operation(self, operation_id: str):
            return cache.get_operation(operation_id)

        def cancel_operation_in_session(self, *args: Any, **kwargs: Any):
            return cache.cancel_operation_in_session(*args, **kwargs)

        def signal_cancelled_operation(self, operation_id: str):
            return cache.signal_cancelled_operation(operation_id)

        def __init__(self) -> None:
            self.recipe_revision_id = recipe_revision_id

    return Adapter()


def _source_artifact(
    root: Path,
    *,
    artifact_id: str,
    path: str,
    content: bytes,
    model_digest: str,
    roles: list[str],
) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    source = root / f"{artifact_id}.source"
    source.write_bytes(content)
    return {
        "id": artifact_id,
        "path": path,
        "kind": "file",
        "source": source.as_uri(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "download_bytes": len(content),
        "roles": roles,
        "model_content_sha256": model_digest,
    }


def _recipe_model_with_assets(
    recipe: RecipeDefinition, *, partial: bytes, shared: bytes
) -> tuple[ModelDefinition, RecipeDefinition]:
    reference = recipe.models[0].model
    model = _canonical_model(
        publisher=reference.publisher,
        slug=reference.slug,
        file_id="weights",
        file_digest=hashlib.sha256(partial).hexdigest(),
    )
    document = model.model_dump(mode="json")
    files = document["files"]
    assert isinstance(files, list) and len(files) == 1
    weight = files[0]
    assert isinstance(weight, dict)
    weight["size_bytes"] = len(partial)
    files.append(
        {
            "id": "shared",
            "path": "shared.bin",
            "roles": ["weights"],
            "sha256": hashlib.sha256(shared).hexdigest(),
            "size_bytes": len(shared),
        }
    )
    model = ModelDefinition.model_validate_json(json.dumps(document))
    recipe_document = recipe.model_dump(mode="json")
    models = recipe_document["models"]
    assert isinstance(models, list) and models
    selected = models[0]
    assert isinstance(selected, dict)
    model_reference = selected["model"]
    assert isinstance(model_reference, dict)
    model_reference["content_sha256"] = content_sha256(model)
    recipe = RecipeDefinition.model_validate_json(json.dumps(recipe_document))
    return model, recipe


def _service(
    sessions: sessionmaker[Session],
    *,
    cache: ModelCacheService,
    recipe: RecipeDefinition,
    recipe_revision_id: str,
    model_digest: str,
    artifacts: list[dict[str, object]],
    image_root: Path,
    now: datetime,
) -> RecipeImageAvailabilityService:
    return RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(image_root),
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        transport=Transport(),
        clock=lambda: now,
        model_cache=_cache_adapter(
            cache,
            recipe_revision_id=recipe_revision_id,
            model_digest=model_digest,
            artifacts=artifacts,
        ),
    )


def test_pending_recipe_child_cancellation_fences_model_removal_and_preserves_peer(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe_seed = _recipe("recipe-image.json")
    partial_bytes = b"unique weights held before publication"
    shared_bytes = b"shr"
    model, recipe = _recipe_model_with_assets(
        recipe_seed, partial=partial_bytes, shared=shared_bytes
    )
    model_digest = content_sha256(model)
    selector = f"{model.identity.publisher}/{model.identity.slug}"
    _register_model(sessions, model)

    peer_model = _canonical_model(
        publisher=model.identity.publisher,
        slug="cache-removal-peer",
        file_id="weights",
        file_digest=hashlib.sha256(shared_bytes).hexdigest(),
    )
    peer_digest = content_sha256(peer_model)
    _register_model(sessions, peer_model)

    recipe_revision_id = uuid.uuid4().hex[:24]
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            CatalogDocument(
                id="document-" + recipe_revision_id,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                title=recipe.metadata.title,
                created_by="operator",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        _add_revision(session, recipe_revision_id, recipe)
        session.add(User(subject="operator", role="operator"))

    cache_root = tmp_path / "model-cache"
    image_root = tmp_path / "image-cache"
    cache = ModelCacheService(
        sessions,
        cache_root,
        reserve_bytes=0,
        fixture_sources=True,
    )
    peer_source_root = tmp_path / "peer-source"
    peer_source_root.mkdir()
    peer_artifact = _artifact(
        peer_source_root,
        shared_bytes,
        model_content_sha256=peer_digest,
    )
    peer_artifact["roles"] = ["weights"]
    peer = _download(
        cache,
        [peer_artifact],
        model_content_sha256=peer_digest,
        request_key=str(uuid.uuid4()),
    )
    assert peer.state == "succeeded", peer.last_error
    peer_set_digest = peer.artifact_set_sha256
    assert peer_set_digest is not None

    shared_artifact = _source_artifact(
        tmp_path / "recipe-source",
        artifact_id="shared",
        path="shared.bin",
        content=shared_bytes,
        model_digest=model_digest,
        roles=["weights"],
    )
    weights_artifact = _source_artifact(
        tmp_path / "recipe-source",
        artifact_id="weights",
        path="weights.safetensors",
        content=partial_bytes,
        model_digest=model_digest,
        roles=["weights"],
    )
    artifacts = [shared_artifact, weights_artifact]
    availability = _service(
        sessions,
        cache=cache,
        recipe=recipe,
        recipe_revision_id=recipe_revision_id,
        model_digest=model_digest,
        artifacts=artifacts,
        image_root=image_root,
        now=now,
    )
    parent = availability.start(
        recipe_revision_id,
        actor="operator",
        request_id=str(uuid.uuid4()),
    )
    assert availability.run_pending() == 1
    parent = availability.get(parent.id)
    assert parent.state == "partial"
    assert parent.model_child is not None
    child_id = str(parent.model_child["id"])
    child = cache.get_operation(child_id)
    assert child.state == "queued"
    target_set_digest = child.artifact_set_sha256
    assert target_set_digest is not None

    shared_digest = hashlib.sha256(shared_bytes).hexdigest()
    partial_digest = hashlib.sha256(partial_bytes).hexdigest()
    shared_object = cache._object_path(shared_digest)
    partial_path = cache._partial_path(target_set_digest, partial_digest)
    assert shared_object.read_bytes() == shared_bytes
    assert cache._stored_object(shared_digest, len(shared_bytes)) == len(shared_bytes)

    process_context = multiprocessing.get_context("spawn")
    reached = process_context.Event()
    release = process_context.Event()
    worker = process_context.Process(
        target=_hold_model_worker,
        args=(
            postgres_engine.url.render_as_string(hide_password=False),
            str(cache_root),
            reached,
            release,
        ),
    )
    cancel_request_id = str(uuid.uuid4())
    removal_request_id = str(uuid.uuid4())
    reason = "cancel this recipe model preparation"
    restarted: ModelCacheService | None = None
    try:
        worker.start()
        assert reached.wait(25), f"model worker missed transfer barrier: {worker.exitcode}"
        assert partial_path.read_bytes() == partial_bytes

        cancelling = availability.cancel(
            parent.id,
            actor="operator",
            request_id=cancel_request_id,
            reason=reason,
        )
        assert cancelling.state == "cancelling"
        availability.reconcile_cancellations()
        assert availability.get(parent.id).state == "cancelling"
        child_cancelling = cache.get_operation(child_id)
        assert child_cancelling.state == "cancelling"
        assert child_cancelling.cancellation is not None
        assert child_cancelling.cancellation["reason"] == reason
        pending_review = cache.review_model_removal(selector)
        assert pending_review.target_identity == model_digest
        assert any(
            finding.owner_kind == "model-cache-operation"
            and finding.owner_id == child_id
            for finding in pending_review.active_work
        )
        assert pending_review.blockers

        with pytest.raises(ModelCacheConflict) as refused:
            cache.remove_model_selector(
                selector,
                actor="operator",
                request_key=removal_request_id,
                model_content_sha256=model_digest,
                review_digest=pending_review.review_digest,
            )
        assert refused.value.code == pending_review.blockers[0].code
        with sessions() as session:
            assert session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == removal_request_id
                )
            ) is None
            assert session.get(ModelCacheSet, target_set_digest) is not None
            assert session.get(ModelCacheSet, peer_set_digest) is not None
            assert session.scalar(
                select(ModelCacheSetArtifact).where(
                    ModelCacheSetArtifact.artifact_set_sha256 == peer_set_digest,
                    ModelCacheSetArtifact.artifact_sha256 == shared_digest,
                )
            ) is not None
        assert shared_object.read_bytes() == shared_bytes
        assert cache._stored_object(shared_digest, len(shared_bytes)) == len(shared_bytes)
        assert partial_path.read_bytes() == partial_bytes

        # The verified partial is still owned by the pending child. Process
        # death releases its artifact lock; the new owner settles cancellation
        # before accepting the fresh removal review.
        worker.terminate()
        worker.join(timeout=10)
        assert worker.exitcode is not None
        restarted = ModelCacheService(
            sessions,
            cache_root,
            reserve_bytes=0,
            fixture_sources=True,
        )
        restarted_availability = _service(
            sessions,
            cache=restarted,
            recipe=recipe,
            recipe_revision_id=recipe_revision_id,
            model_digest=model_digest,
            artifacts=artifacts,
            image_root=image_root,
            now=now,
        )
        assert restarted._reconcile_pending_cancellations() == 1
        assert restarted_availability.reconcile_cancellations() >= 1
        assert restarted.get_operation(child_id).state == "cancelled"
        assert restarted_availability.get(parent.id).state == "cancelled"
        assert partial_path.read_bytes() == partial_bytes

        # A fresh review after cancellation settles permits a new explicit
        # removal, while object ownership retains the peer's verified bytes.
        settled_review = restarted.review_model_removal(selector)
        assert settled_review.target_identity == model_digest
        assert not settled_review.active_work
        assert not settled_review.blockers
        assert settled_review.review_digest != pending_review.review_digest
        accepted = restarted.remove_model_selector(
            selector,
            actor="operator",
            request_key=str(uuid.uuid4()),
            model_content_sha256=model_digest,
            review_digest=settled_review.review_digest,
        )
        for _ in range(16):
            current = restarted.get_operation(accepted.id)
            if current.state in {"succeeded", "failed", "cancelled"}:
                break
            restarted.advance_removals(limit=1)
        settled = restarted.get_operation(accepted.id)
        assert settled.state == "succeeded", settled.last_error
        with sessions() as session:
            assert session.get(ModelCacheSet, target_set_digest) is None
            assert session.get(ModelCacheSet, peer_set_digest) is not None
        assert shared_object.read_bytes() == shared_bytes
        assert restarted._stored_object(shared_digest, len(shared_bytes)) == len(
            shared_bytes
        )
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(timeout=10)
        if restarted is not None:
            restarted.close()
        cache.close()
