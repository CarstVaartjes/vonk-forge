"""Damage in one saved owner retains its scope while unrelated work advances."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.artifact_reference_scan import (
    model_set_reference_findings,
    runtime_image_reference_findings,
)
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    CatalogDocumentHead,
    CatalogDocumentRevision,
    FleetProfile,
)
from vonk_forge_contracts import document_sha256, read_recipe

from .test_catalog_revision_collection import Catalog
from .test_model_cache import _artifact, _canonical_recipe, _download
from .test_unused_storage_collection import (
    A_MODEL,
    AN_IMAGE,
    _collector,
    _models,
    _profile,
    _receipt,
    _set_exists,
    _settle,
)
from .test_unused_storage_collection import (
    world as world,  # noqa: PLC0414 - shared fixture
)


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("damage", ["draft", "head", "model"])
def test_scoped_profile_damage_keeps_exact_content_and_collects_unrelated_model(
    world: Catalog, tmp_path: Path, damage: str
) -> None:
    """Catches a missing model/head or damaged options vetoing every removal."""
    recipe = _canonical_recipe(A_MODEL)
    identity = recipe["identity"]
    assert isinstance(identity, dict)
    identity["publisher"] = "vonk-forge"
    identity["slug"] = "scoped"
    definition = read_recipe(recipe)
    document = definition.model_dump(mode="json", exclude_none=True)
    revision_id = world.revision("scoped", 1, head="active", document=document)
    # Catalog.revision deliberately gives its historical rows a mismatched
    # digest. This scenario needs readable recipe content to scope protection
    # independently of the damaged draft/head or absent model projection.
    with world.sessions.begin() as session:
        revision = session.get(CatalogDocumentRevision, revision_id)
        assert revision is not None
        revision.content_digest = document_sha256(document)
    _profile(world, "vonk-forge/scoped")
    if damage == "draft":
        with world.sessions.begin() as session:
            profile = session.scalar(select(FleetProfile))
            assert profile is not None
            profile.assignments = [
                {
                    "recipe_selector": "vonk-forge/scoped",
                    "spark_ids": None,
                    "option_choices": 7,
                }
            ]
    elif damage == "head":
        with world.sessions.begin() as session:
            head = session.scalar(select(CatalogDocumentHead))
            assert head is not None
            head.active_revision_id = None
    # No model revision exists: the verified recipe's content pin still scopes
    # the reference, including when its saved draft or active head is damaged.
    service = ModelCacheService(
        world.sessions, tmp_path / "cache", reserve_bytes=0, fixture_sources=True
    )
    try:
        protected = _download(
            service,
            [_artifact(tmp_path, b"protected", model_content_sha256=A_MODEL)],
            model_content_sha256=A_MODEL,
            request_key=str(uuid.uuid4()),
        )
        unrelated = _download(
            service,
            [_artifact(tmp_path, b"unrelated", model_content_sha256="b" * 64)],
            model_content_sha256="b" * 64,
            request_key=str(uuid.uuid4()),
        )
        assert protected.artifact_set_sha256 is not None
        assert unrelated.artifact_set_sha256 is not None
        with world.sessions() as session:
            findings = model_set_reference_findings(
                session, (protected.artifact_set_sha256, unrelated.artifact_set_sha256)
            )
        assert findings[protected.artifact_set_sha256]
        assert not findings[unrelated.artifact_set_sha256]
        _models(world, service).collect()
        _settle(service)
        assert _set_exists(world, protected.artifact_set_sha256)
        assert not _set_exists(world, unrelated.artifact_set_sha256)
        fresh = _download(
            service,
            [_artifact(tmp_path, b"unrelated", model_content_sha256="b" * 64)],
            model_content_sha256="b" * 64,
            request_key=str(uuid.uuid4()),
        )
        assert fresh.artifact_set_sha256 is not None
        assert _set_exists(world, fresh.artifact_set_sha256)
    finally:
        service.close()


@pytest.mark.usefixtures("damaged_json_rows")
def test_unrecoverable_selector_retains_images_then_fresh_collection_after_owner_repair(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches treating a damaged stored selector as proof of no reference."""
    path = _receipt(tmp_path, AN_IMAGE)
    _profile(world, "missing-publisher")
    with world.sessions() as session:
        assert runtime_image_reference_findings(session, (AN_IMAGE,))[AN_IMAGE]
    collector = _collector(world, image_cache_root=tmp_path)
    assert collector.collect().images == 0
    assert path.exists()
    # Authoritative owner repair establishes that the profile has no choices.
    with world.sessions.begin() as session:
        profile = session.scalar(select(FleetProfile))
        assert profile is not None
        profile.assignments = []
    assert collector.collect().images == 1
    assert not path.exists()
    fresh = _receipt(tmp_path, "e" * 64)
    assert _collector(world, image_cache_root=tmp_path).collect().images == 1
    assert not fresh.exists()


@pytest.mark.usefixtures("damaged_json_rows")
def test_missing_active_head_retains_its_images_without_stalling_unrelated_receipt(
    world: Catalog, tmp_path: Path
) -> None:
    """Catches failing a whole scan when one selector's active head is absent."""
    revision = world.revision("scoped", 1, state="failed")
    world.build(revision, archive=AN_IMAGE)
    protected = _receipt(tmp_path, AN_IMAGE)
    other = _receipt(tmp_path, "e" * 64)
    _profile(world, "vonk-forge/scoped")
    collector = _collector(world, image_cache_root=tmp_path)
    assert collector.collect().images == 1
    assert protected.exists()
    assert not other.exists()
    with world.sessions.begin() as session:
        profile = session.scalar(select(FleetProfile))
        assert profile is not None
        profile.assignments = []
    assert _collector(world, image_cache_root=tmp_path).collect().images == 1
    assert not protected.exists()
