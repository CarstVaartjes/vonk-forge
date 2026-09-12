from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_repository import CatalogRepository
from vonk_control.catalog_service import CatalogService, CatalogValidationError
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base, CatalogDocumentHead, CatalogDocumentRevision
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from .test_catalog_entities import _model, _recipe

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _document_section(document: dict[str, object], key: str) -> dict[str, object]:
    """Narrow one decoded JSON object member so a deliberate edit stays typed."""

    section = document[key]
    assert isinstance(section, dict)
    return section


@pytest.fixture(params=[False, True], ids=["forward-scan", "reverse-scan"])
def catalog(tmp_path, request):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")

    @event.listens_for(engine, "connect")
    def set_scan_order(connection, _record):
        # Neither insertion order nor an index's scan direction selects a head.
        connection.execute(f"PRAGMA reverse_unordered_selects = {int(request.param)}")

    Base.metadata.create_all(engine)
    return CatalogService(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: NOW,
        cursors=TokenCodec(b"h" * 32).cursor_codec(),
    )


def _publish(catalog, document):
    draft = catalog.entities.create_draft(document, actor="test")
    return catalog.entities.resolve(draft.id, actor="test")


def _library(catalog):
    return LibraryProjection(catalog._sessions, cursors=catalog._cursors, clock=lambda: NOW)


def _assert_current(catalog, first, expected):
    detail = _library(catalog).authoring_recipe_detail(first.document_id)
    assert detail.recipe.recipe_revision_id == expected.id
    assert detail.recipe.content_sha256 == expected.content_digest
    assert detail.definition == RecipeDefinition.model_validate(expected.document)
    assert catalog.get_recipe(first.document_id).id == expected.id
    current = catalog.recipe_catalog_local_revisions([(first.publisher, first.slug)])
    assert current[(first.publisher, first.slug)].content_sha256 == expected.content_digest
    library = _library(catalog)
    assert {
        row.identity.recipe_revision_id
        for row in library.recipe_library(all_models=True).recipes
    } == {expected.id}
    assert library.recipe_detail(f"{first.publisher}/{first.slug}").identity.recipe_revision_id == expected.id
    # A cached historical revision must not reappear as another Library choice.
    assert {
        row.id for row in library._catalog_documents(
            kind="recipe", local_digests=[first.content_digest]
        )
    } == {expected.id}
    with catalog._sessions() as session:
        active = CatalogRepository().active_revision(session, first.document_id)
        assert active is not None
        assert active.id == expected.id
        assert ModelCacheService._latest_recipe_digest(session, first.content_digest) == expected.content_digest


def test_stable_recipe_reads_follow_promotion_and_ignore_failed_successor(catalog):
    model = _model()
    _publish(catalog, model)
    first = _publish(catalog, _recipe(model))
    _assert_current(catalog, first, first)

    changed = copy.deepcopy(first.document)
    changed["metadata"]["title"] = "Accepted successor"
    candidate = catalog.entities.revise(first.document_id, changed, actor="test")
    _assert_current(catalog, first, first)
    second = catalog.entities.resolve(candidate.id, actor="test")
    _assert_current(catalog, first, second)

    invalid = copy.deepcopy(second.document)
    invalid["models"][0]["model"]["content_sha256"] = "f" * 64
    failed = catalog.entities.revise(first.document_id, invalid, actor="test")
    with pytest.raises(CatalogValidationError):
        catalog.entities.resolve(failed.id, actor="test")
    catalog.entities.fail_candidate(first.document_id, reason="missing exact model")
    _assert_current(catalog, first, second)

    # Acceptance remains revision-specific: historical exact reads survive a
    # head change, while Library offers only the current accepted recipe.
    assert catalog.get_recipe(first.id).content_sha256 == first.content_digest
    with pytest.raises(KeyError):
        catalog.get_recipe(failed.id)
    with catalog._sessions() as session:
        assert session.get(CatalogDocumentRevision, first.id).state == "active"


def test_recipe_detail_keeps_its_exact_model_when_model_head_advances(catalog):
    model = _model()
    first_model = _publish(catalog, model)
    recipe = _publish(catalog, _recipe(model))
    changed = copy.deepcopy(model)
    _document_section(changed, "metadata")["description"] = "New model metadata"
    candidate = catalog.entities.revise(first_model.document_id, changed, actor="test")
    catalog.entities.resolve(candidate.id, actor="test")

    detail = _library(catalog).authoring_recipe_detail(recipe.document_id)
    assert detail.model_documents[0].model_document == ModelDefinition.model_validate(model)
    assert catalog.entities.resolve_reference(
        RecipeDefinition.model_validate(recipe.document).models[0].model
    ).id == first_model.id


def test_stable_recipe_reads_do_not_guess_when_active_head_is_missing(catalog):
    model = _model()
    _publish(catalog, model)
    first = _publish(catalog, _recipe(model))
    with catalog._sessions.begin() as session:
        head = session.scalar(select(CatalogDocumentHead).where(
            CatalogDocumentHead.active_revision_id == first.id
        ))
        head.active_revision_id = None

    with pytest.raises(KeyError):
        _library(catalog).authoring_recipe_detail(first.document_id)
    with pytest.raises(KeyError):
        catalog.get_recipe(first.document_id)
    assert catalog.recipe_catalog_local_revisions([(first.publisher, first.slug)]) == {}
    assert _library(catalog).recipe_library(all_models=True).recipes == []
    assert catalog.get_recipe(first.id).id == first.id
    with catalog._sessions() as session:
        assert CatalogRepository().active_revision(session, first.document_id) is None
        assert ModelCacheService._latest_recipe_digest(session, first.content_digest) is None
