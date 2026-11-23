"""Model preparation finds the target's active operation, however old it is."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    Base,
    CatalogDocument,
    ModelCacheOperation,
    User,
)
from vonk_forge_contracts import document_sha256

from .test_cache_removal_cancellation_postgres import (
    _recipe_model_with_assets,
    _service,
    _source_artifact,
)
from .test_model_removal_reference_lifecycle import _register_model
from .test_recipe_image_availability import _add_revision, _recipe

NEWER_UNRELATED = 120


def test_older_active_operation_is_reused_behind_many_newer_unrelated_ones(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    partial, shared = b"unique weights of the target", b"shr"
    model, recipe = _recipe_model_with_assets(
        _recipe("recipe-source-build.json"), partial=partial, shared=shared
    )
    model_digest = document_sha256(model.model_dump(mode="json"))
    _register_model(sessions, model)
    revision_id = uuid.uuid4().hex[:24]
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            CatalogDocument(
                id="document-" + revision_id,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                title=recipe.identity.slug,
                created_by="operator",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        _add_revision(session, revision_id, recipe)
        session.add(User(subject="operator", role="operator"))
    cache = ModelCacheService(
        sessions, tmp_path / "model-cache", free_fraction=0, fixture_sources=True
    )
    artifacts = [
        _source_artifact(
            tmp_path / "source",
            artifact_id=name,
            path=path,
            content=content,
            model_digest=model_digest,
            roles=["weights"],
        )
        for name, path, content in [
            ("shared", "shared.bin", shared),
            ("weights", "weights.safetensors", partial),
        ]
    ]
    availability = _service(
        sessions,
        cache=cache,
        recipe=recipe,
        recipe_revision_id=revision_id,
        model_digest=model_digest,
        artifacts=artifacts,
        image_root=tmp_path / "image-cache",
        now=now,
    )
    first = availability.start(
        revision_id, actor="operator", request_id=str(uuid.uuid4())
    )
    assert availability.run_pending() == 1
    first = availability.get(first.id)
    assert first.model_child is not None
    child_id = str(first.model_child["id"])
    child = cache.get_operation(child_id)
    assert child.state == LifecycleState.QUEUED

    # More operations than the old 100-row window, all newer and all unrelated.
    with sessions.begin() as session:
        template = session.get(ModelCacheOperation, child_id)
        assert template is not None
        for index in range(NEWER_UNRELATED):
            session.add(
                ModelCacheOperation(
                    id=str(uuid.uuid4()),
                    request_key=str(uuid.uuid4()),
                    kind="download",
                    state=LifecycleState.SUCCEEDED.value,
                    artifact_set_sha256=None,
                    payload=template.payload,
                    progress=template.progress,
                    actor="operator",
                    created_at=template.created_at + timedelta(seconds=index + 1),
                    updated_at=template.updated_at,
                )
            )

    second = availability.start(
        revision_id, actor="operator-2", request_id=str(uuid.uuid4())
    )
    assert availability.run_pending() == 1
    reused = availability.get(second.id).model_child
    assert reused is not None
    assert reused["id"] == child_id
    with sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ModelCacheOperation)
                .where(
                    ModelCacheOperation.artifact_set_sha256 == child.artifact_set_sha256
                )
            )
            == 1
        )

    # A removal of the same set is never reused.
    with sessions.begin() as session:
        row = session.get(ModelCacheOperation, child_id)
        assert row is not None
        row.kind = "remove"
    assert (
        cache.reusable_operation(
            artifact_set_sha256=str(child.artifact_set_sha256),
            include_succeeded=True,
        )
        is None
    )
    with sessions.begin() as session:
        row = session.get(ModelCacheOperation, child_id)
        assert row is not None
        row.kind = "download"

    # An operation whose cancellation is being driven is not rejoined.
    with sessions.begin() as session:
        assert cache.cancel_operation_in_session(
            session,
            child_id,
            actor="operator",
            request_key=str(uuid.uuid4()),
            reason="no longer wanted",
        )
    assert cache.get_operation(child_id).state == LifecycleState.OBSERVING
    assert (
        cache.reusable_operation(
            artifact_set_sha256=str(child.artifact_set_sha256),
            include_succeeded=True,
        )
        is None
    )

    # A fresh request after the cancel starts a new child instead of joining it.
    third = availability.start(
        revision_id, actor="operator-3", request_id=str(uuid.uuid4())
    )
    availability.run_pending()
    fresh_child = availability.get(third.id).model_child
    assert fresh_child is not None and fresh_child["id"] != child_id
