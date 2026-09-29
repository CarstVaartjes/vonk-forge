"""An exact manifest resolves from the newest readable revision, not an old one."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vonk_control.model_cache import ModelCacheService
from vonk_control.models import CatalogDocumentRevision

from .test_canonical_cache_build_identity import (
    _add_active,
    _digest,
    _model_document,
    _recipe_document,
    _sessions,
)

DOCUMENT = "00000000-0000-4000-8000-0000000000a1"
MODEL_DOCUMENT = "00000000-0000-4000-8000-0000000000b1"


def _unreadable(revision: CatalogDocumentRevision) -> str:
    """Rewrite one new revision as written under an older contract."""

    revision.document = {"kind": "old"}
    revision.content_digest = hashlib.sha256(
        json.dumps(
            revision.document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return revision.content_digest


def test_unreadable_recipe_revision_resolves_from_newest_readable(tmp_path: Path):
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
        edited["metadata"]["description"] = "Updated release description."
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

    manifest = ModelCacheService(
        sessions, tmp_path / "cache", reserve_bytes=0
    ).resolve_artifact_set(recipe_revision_id=old_id)

    assert manifest.recipe_revision_sha256 == current_digest
    assert manifest.model_content_sha256 == model_digest
    assert manifest.artifacts[0].expected_bytes == 12


def test_unreadable_model_revision_resolves_from_newest_readable(tmp_path: Path):
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

    manifest = ModelCacheService(
        sessions, tmp_path / "cache", reserve_bytes=0
    ).resolve_artifact_set(recipe_revision_id=recipe_id)

    assert manifest.model_content_sha256 == new_digest
    assert manifest.model_content_digests == (new_digest,)
    assert manifest.artifacts[0].path == "weights/new.safetensors"
