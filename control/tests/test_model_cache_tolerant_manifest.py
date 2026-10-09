"""Damaged catalog projections never substitute different requested content."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import CatalogDocumentRevision

from .test_canonical_cache_build_identity import (
    _add_active,
    _digest,
    _model_document,
    _recipe_document,
    _sessions,
)
from .test_model_cache_recovery_support import observe_unknown

DOCUMENT = "00000000-0000-4000-8000-0000000000a1"
MODEL_DOCUMENT = "00000000-0000-4000-8000-0000000000b1"


def _unreadable(revision: CatalogDocumentRevision) -> str:
    """Rewrite one new revision as written under an older contract."""

    revision.document = {"kind": "old"}
    return revision.content_digest


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("different_content", [False, True])
def test_unreadable_recipe_revision_recovers_only_identical_content(
    tmp_path: Path, different_content: bool
):
    sessions = _sessions()
    file_digest = hashlib.sha256(b"model bytes!").hexdigest()
    model_document = _model_document(
        path="weights/model.safetensors", file_digest=file_digest, roles=["weights"]
    )
    model_digest = _digest(model_document)
    recipe_document = _recipe_document(model_digest)
    with sessions.begin() as session:
        _add_active(
            session,
            root_id=MODEL_DOCUMENT,
            revision_id="00000000-0000-4000-8000-0000000000b2",
            kind="model",
            publisher="owner",
            slug="model",
            document=model_document,
        )
        old = _add_active(
            session,
            root_id=DOCUMENT,
            revision_id="00000000-0000-4000-8000-0000000000a2",
            kind="recipe",
            publisher="owner",
            slug="recipe",
            document=recipe_document,
        )
        edited = json.loads(json.dumps(recipe_document))
        if different_content:
            edited["metadata"]["description"] = "different accepted content"
        current = _add_active(
            session,
            root_id=DOCUMENT,
            revision_id="00000000-0000-4000-8000-0000000000a3",
            kind="recipe",
            publisher="owner",
            slug="recipe",
            document=edited,
            revision_number=2,
        )
        _unreadable(old)
        old_id, current_digest = old.id, current.content_digest

    service = ModelCacheService(sessions, tmp_path / "cache", reserve_bytes=0)
    try:
        if different_content:
            with observe_unknown():
                observed = service.resolve_artifact_set(recipe_revision_id=old_id)
                assert not observed
            with sessions.begin() as session:
                exact = session.get(CatalogDocumentRevision, old_id)
                assert exact is not None
                exact.document = recipe_document
        manifest = service.resolve_artifact_set(recipe_revision_id=old_id)
        assert manifest.recipe_revision_sha256 == _digest(recipe_document)
        if different_content:
            assert manifest.recipe_revision_sha256 != current_digest
        assert manifest.model_content_sha256 == model_digest
        assert manifest.artifacts[0].expected_bytes == 12
    finally:
        service.close()


@pytest.mark.usefixtures("damaged_json_rows")
def test_unreadable_model_revision_never_substitutes_new_content(tmp_path: Path):
    sessions = _sessions()
    file_digest = hashlib.sha256(b"model bytes!").hexdigest()
    old_model = _model_document(
        path="weights/old.safetensors", file_digest=file_digest, roles=["weights"]
    )
    new_model = _model_document(
        path="weights/new.safetensors", file_digest=file_digest, roles=["weights"]
    )
    new_digest = _digest(new_model)
    with sessions.begin() as session:
        old = _add_active(
            session,
            root_id=MODEL_DOCUMENT,
            revision_id="00000000-0000-4000-8000-0000000000b2",
            kind="model",
            publisher="owner",
            slug="model",
            document=old_model,
        )
        _add_active(
            session,
            root_id=MODEL_DOCUMENT,
            revision_id="00000000-0000-4000-8000-0000000000b3",
            kind="model",
            publisher="owner",
            slug="model",
            document=new_model,
            revision_number=2,
        )
        pinned = _unreadable(old)
        recipe = _add_active(
            session,
            root_id=DOCUMENT,
            revision_id="00000000-0000-4000-8000-0000000000a2",
            kind="recipe",
            publisher="owner",
            slug="recipe",
            document=_recipe_document(pinned),
        )
        recipe_id = recipe.id

    service = ModelCacheService(sessions, tmp_path / "cache", reserve_bytes=0)
    try:
        # The bounded request ends with no manifest; no new path is adopted.
        with observe_unknown():
            observed = service.resolve_artifact_set(recipe_revision_id=recipe_id)
            assert not observed
        with sessions.begin() as session:
            exact = session.get(
                CatalogDocumentRevision, "00000000-0000-4000-8000-0000000000b2"
            )
            assert exact is not None
            exact.document = old_model
        fresh = service.resolve_artifact_set(recipe_revision_id=recipe_id)
        assert fresh.model_content_sha256 == pinned
        assert fresh.model_content_sha256 != new_digest
        assert fresh.artifacts[0].path == "weights/old.safetensors"
    finally:
        service.close()


@pytest.mark.usefixtures("damaged_json_rows")
def test_an_installation_of_an_unreadable_revision_does_not_stop_inspection(
    tmp_path: Path,
):
    """The live case: an old installation still names an unreadable revision.

    Cache protection reads every installation's recipe while the download
    preview measures storage; that read must resolve through the newest
    readable revision instead of failing artifact inspection for every load.
    """

    from datetime import UTC, datetime

    from vonk_control.models import ModelCacheSet, RecipeInstallation
    from vonk_control.run_switch_operations import DatabaseRunSwitchArtifactInspector

    sessions = _sessions()
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    file_digest = hashlib.sha256(b"model bytes!").hexdigest()
    model_document = _model_document(
        path="weights/model.safetensors", file_digest=file_digest, roles=["weights"]
    )
    model_digest = _digest(model_document)
    recipe_document = _recipe_document(model_digest)
    with sessions.begin() as session:
        _add_active(
            session,
            root_id=MODEL_DOCUMENT,
            revision_id="00000000-0000-4000-8000-0000000000b2",
            kind="model",
            publisher="owner",
            slug="model",
            document=model_document,
        )
        old = _add_active(
            session,
            root_id=DOCUMENT,
            revision_id="00000000-0000-4000-8000-0000000000a2",
            kind="recipe",
            publisher="owner",
            slug="recipe",
            document=recipe_document,
        )
        edited = json.loads(json.dumps(recipe_document))
        current = _add_active(
            session,
            root_id=DOCUMENT,
            revision_id="00000000-0000-4000-8000-0000000000a3",
            kind="recipe",
            publisher="owner",
            slug="recipe",
            document=edited,
            revision_number=2,
        )
        _unreadable(old)
        session.add(
            RecipeInstallation(
                id="00000000-0000-4000-8000-0000000000c1",
                recipe_revision_id=old.id,
                mapping_id="00000000-0000-4000-8000-0000000000c2",
                mapping_generation=1,
                image_digest="sha256:" + "1" * 64,
                plan_digest="2" * 64,
                plan={"kind": "old"},
                state="installed",
                actor="admin",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ModelCacheSet(
                artifact_set_sha256="3" * 64,
                model_content_sha256=model_digest,
                manifest={"kind": "old"},
                expected_bytes=12,
                state="incomplete",
                created_at=now,
                updated_at=now,
                last_accessed_at=now,
            )
        )
        current_id = current.id

    cache = ModelCacheService(sessions, tmp_path / "cache", reserve_bytes=0)
    with sessions() as session:
        inspection = DatabaseRunSwitchArtifactInspector(cache).inspect(
            session,
            model_content_sha256=model_digest,
            recipe_revision_id=current_id,
            node_ids=(),
            retention="keep-cached",
            now=now,
        )

    assert inspection.artifact_set_bytes == 12
    # Inspection only reads; protection is recomputed where the cache reports its
    # storage, and an installation of an unreadable revision still protects.
    cache.storage_summary()
    with sessions() as session:
        row = session.get(ModelCacheSet, "3" * 64)
        assert row is not None and "recipe-installation" in row.protected_reasons
