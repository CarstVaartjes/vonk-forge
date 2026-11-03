"""Contracts 3.0 moved display out of the documents; stored 2.x rows stay usable.

A stored row keeps the 2.x bytes it was written with and is read by the one
tolerant reader. Nothing writes a 2.x shape again, and a stored projection in
the old shape is re-derived without losing what the catalog sync recorded.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from importlib import resources
from typing import cast

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import CursorCodec
from vonk_control.catalog_entities import (
    CatalogCode,
    CatalogEntityService,
    CatalogValidationError,
    _parse,
    recipe_document_projection,
)
from vonk_control.catalog_revision_contract import (
    ModelRevisionProjection,
    PublishedDocument,
    RecipeRevisionProjection,
    read_catalog_document,
    read_catalog_projection,
)
from vonk_control.catalog_service import CatalogService
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
)
from vonk_control.recipe_update_notice import (
    newest_active_revisions,
    recipe_update_notice,
)
from vonk_forge_contracts import document_sha256, read_model, read_recipe

from .display_fixtures import create_draft, display_of

_EXAMPLES = resources.files("vonk_forge_contracts") / "examples"
NOW = datetime(2026, 11, 2, tzinfo=UTC)
_PACKAGE_HANDLE = {
    "publication_commit": "a" * 40,
    "source_commit": "b" * 40,
    "package_sha256": "c" * 64,
    "package_size": 10,
    "package_path": "packages/synthetic.tar.gz",
    "recipe_content_sha256": "d" * 64,
    "archive_path": "/cache/synthetic.tar.gz",
    "closure_path": "/cache/synthetic/closure",
}
_ARTIFACT_INPUTS = [{"selection_id": "primary", "artifact_key": "9" * 64}]
_MODEL_ARTIFACTS = [{"path": "model/weights.bin", "sha256": "8" * 64, "size_bytes": 3}]
_PREBUILT = {
    "reference": "registry.example/vonk/recipe@sha256:" + "e" * 64,
    "build_key": "f" * 64,
}


def _example(name: str) -> dict:
    return json.loads((_EXAMPLES / name).read_text(encoding="utf-8"))


def _legacy_model() -> dict:
    """The Model document as contracts 2.x published it, display inside."""
    document = _example("model-definition.json")
    identity = document["identity"]
    identity["family"]["title"] = "Synthetic"
    identity["model"]["title"] = "Synthetic Tiny"
    document["metadata"] = {"description": "A small model.", "tags": ["synthetic"]}
    return document


def _legacy_recipe() -> dict:
    document = _example("recipe-source-build.json")
    document["metadata"] = {
        "title": "Synthetic Tiny build",
        "description": "Synthetic source-build example.",
        "tags": ["synthetic"],
    }
    return document


def _legacy_projection(document: dict, kind: str) -> dict:
    """The projection a 2.x Controller stored, with what its sync recorded."""
    if kind == "model":
        model = read_model(document)
        return {
            "identity": document["identity"],
            "metadata": document["metadata"],
            "format": document["format"],
            "capabilities": document["capabilities"],
            "modalities": document["modalities"],
            "artifact_count": len(model.files),
            "download_bytes": model.download_bytes,
            "installed_bytes": model.installed_bytes,
        }
    projected = recipe_document_projection(read_recipe(document))
    metadata = document["metadata"]
    return {
        **projected,
        "metadata": metadata,
        "title": metadata["title"],
        "description": metadata["description"],
        "tags": metadata["tags"],
        "package_handle": _PACKAGE_HANDLE,
        "prebuilt_image": _PREBUILT,
        "release_version": "1.0.0",
        "publication_commit": "a" * 40,
        "artifact_inputs": _ARTIFACT_INPUTS,
        "build_model_artifacts": _MODEL_ARTIFACTS,
    }


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _store(sessions, document: dict, kind: str, *, projected: dict) -> str:
    """Insert a revision exactly as a 2.x Controller wrote it."""
    with sessions.begin() as session:
        root = CatalogDocument(
            kind=kind,
            publisher=document["identity"]["publisher"],
            slug=document["identity"]["slug"],
            title="Stored title",
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(root)
        session.flush()
        revision = CatalogDocumentRevision(
            document_id=root.id,
            kind=kind,
            publisher=root.publisher,
            slug=root.slug,
            revision_number=1,
            schema_version=2,
            state="active",
            document={},
            content_digest=document_sha256(document),
            artifact_key="1" * 64,
            execution_key="2" * 64,
            projected={},
            created_by="admin",
            created_at=NOW,
        )
        session.add(revision)
        session.flush()
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision.id)
            .values(document=document, projected=projected)
        )
        return revision.id


def test_a_stored_2x_row_is_read_and_rederived_by_the_sync_keeping_what_sync_recorded(
    sessions,
) -> None:
    recipe, model = _legacy_recipe(), _legacy_model()
    recipe_id = _store(
        sessions, recipe, "recipe", projected=_legacy_projection(recipe, "recipe")
    )
    model_id = _store(
        sessions, model, "model", projected=_legacy_projection(model, "model")
    )
    unreadable = copy.deepcopy(_example("recipe-job.json"))
    unreadable_id = _store(sessions, unreadable, "recipe", projected={"broken": True})
    with sessions.begin() as session:
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == unreadable_id)
            .values(document={"broken": True})
        )
    with sessions() as session:
        before = {
            row.id: (
                row.content_digest,
                row.artifact_key,
                row.execution_key,
                row.document,
            )
            for row in session.scalars(select(CatalogDocumentRevision))
        }
        # A read before the sweep already repairs the projection, losing nothing.
        row = session.get(CatalogDocumentRevision, recipe_id)
        read = read_catalog_projection(row)
        assert isinstance(read, RecipeRevisionProjection)
        assert read.package_handle is not None
        assert read.package_handle.package_sha256 == "c" * 64
        assert read.prebuilt_image is not None
        assert read.prebuilt_image.build_key == "f" * 64
        assert read_catalog_document(row).identity.slug == recipe["identity"]["slug"]

    entities = CatalogEntityService(sessions, clock=lambda: NOW)
    entities.refresh_build_policy()
    entities.refresh_model_projections()
    with sessions() as session:
        settled = {
            row.id: row.projected
            for row in session.scalars(select(CatalogDocumentRevision))
        }
    entities.refresh_build_policy()

    with sessions() as session:
        recipe_row = session.get(CatalogDocumentRevision, recipe_id)
        model_row = session.get(CatalogDocumentRevision, model_id)
        for retired in ("metadata", "title", "description", "tags"):
            assert retired not in recipe_row.projected
        assert "metadata" not in model_row.projected
        assert recipe_row.projected["package_handle"] == _PACKAGE_HANDLE
        assert recipe_row.projected["prebuilt_image"] == _PREBUILT
        assert recipe_row.projected["release_version"] == "1.0.0"
        assert recipe_row.projected["artifact_inputs"] == _ARTIFACT_INPUTS
        assert recipe_row.projected["build_model_artifacts"] == _MODEL_ARTIFACTS
        assert isinstance(read_catalog_projection(model_row), ModelRevisionProjection)
        # Identity, build keys and bytes of every row are untouched: nothing
        # rebuilds, redownloads or re-keys.
        after = {
            row.id: (
                row.content_digest,
                row.artifact_key,
                row.execution_key,
                row.document,
            )
            for row in session.scalars(select(CatalogDocumentRevision))
        }
        assert after == before
        # A second sync pass finds nothing to rewrite.
        assert {
            row.id: row.projected
            for row in session.scalars(select(CatalogDocumentRevision))
        } == settled
        # The unreadable row was logged and skipped; it never blocked the rest.
        assert session.get(CatalogDocumentRevision, unreadable_id).projected == {
            "broken": True
        }


@pytest.mark.parametrize(
    "legacy",
    [
        pytest.param(_legacy_recipe, id="recipe-metadata"),
        pytest.param(_legacy_model, id="model-metadata-and-titles"),
    ],
)
def test_no_write_path_stores_a_2x_shape(sessions, legacy) -> None:
    document = legacy()
    entities = CatalogEntityService(sessions, clock=lambda: NOW)
    service = CatalogService(
        sessions, clock=lambda: NOW, cursors=CursorCodec(b"c" * 32)
    )
    clean = _example(
        "model-definition.json" if document["kind"] == "model" else "recipe-job.json"
    )
    clean["identity"]["slug"] = document["identity"]["slug"]
    seed = create_draft(entities, clean, actor="test")

    with pytest.raises(CatalogValidationError) as drafted:
        create_draft(entities, document, actor="test")
    assert drafted.value.code == CatalogCode.DOCUMENT_INVALID
    with pytest.raises(CatalogValidationError) as revised:
        entities.revise(seed.document_id, document, actor="test")
    assert revised.value.code == CatalogCode.DOCUMENT_INVALID
    with pytest.raises(CatalogValidationError) as imported:
        if document["kind"] == "model":
            service.import_catalog_models("test", [document])
        else:
            service.import_recipe_library(
                "test",
                library_commit="a" * 40,
                source_path="recipe.json",
                document=document,
                expected_content_sha256=document_sha256(document),
            )
    assert imported.value.code in {
        CatalogCode.DOCUMENT_INVALID,
        CatalogCode.MODEL_DOCUMENT_INVALID,
    }
    with sessions() as session:
        stored = session.scalars(select(CatalogDocumentRevision)).all()
        assert [row.id for row in stored] == [seed.id]


def test_a_3_0_document_with_a_newer_minor_field_is_accepted() -> None:
    document = _example("recipe-job.json")
    document["a_field_a_newer_minor_added"] = {"x": 1}
    parsed, clean, *_ = _parse(document)
    assert clean == document and parsed.identity.slug == document["identity"]["slug"]


def test_a_display_must_describe_its_own_document(sessions) -> None:
    document = _example("recipe-job.json")
    other = copy.deepcopy(document)
    other["identity"]["slug"] = "somebody-else"
    entities = CatalogEntityService(sessions, clock=lambda: NOW)
    with pytest.raises(CatalogValidationError) as refused:
        entities.create_draft(
            cast(PublishedDocument, document), display_of(other), actor="test"
        )
    assert refused.value.code == CatalogCode.DOCUMENT_INVALID


def test_the_shared_document_vectors_agree_with_the_controller(sessions) -> None:
    """Every valid vector is accepted; every invalid document vector is refused."""
    entities = CatalogEntityService(sessions, clock=lambda: NOW)
    valid = [
        path.name
        for path in _EXAMPLES.iterdir()
        if path.name.endswith(".json") and "display" not in path.name
    ]
    assert valid
    for name in valid:
        document = _example(name)
        create_draft(entities, document, actor="test")
    refused = 0
    for path in (_EXAMPLES / "invalid").iterdir():
        if path.name.endswith(".expect.json"):
            continue
        expected = json.loads(
            (
                _EXAMPLES / "invalid" / path.name.replace(".json", ".expect.json")
            ).read_text()
        )
        if expected["contract"] not in {"RecipeDefinition", "ModelDefinition"}:
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        with pytest.raises(CatalogValidationError) as error:
            create_draft(entities, document, actor="test")
        assert error.value.code == CatalogCode.DOCUMENT_INVALID
        refused += 1
    assert refused, "the shared vectors name document vectors to refuse"


def test_a_row_the_3_0_controller_wrote_is_already_current(sessions) -> None:
    """A resolved recipe keeps its bindings: the reconcile has nothing to rewrite."""
    entities = CatalogEntityService(sessions, clock=lambda: NOW)
    model = _example("model-definition.json")
    recipe = _example("recipe-source-build.json")
    recipe["models"][0]["model"]["content_sha256"] = document_sha256(model)
    for document in (model, recipe):
        draft = create_draft(entities, document, actor="test")
        entities.resolve(draft.id, actor="test")
    with sessions() as session:
        stored = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        assert stored is not None and stored.projected["artifact_inputs"]
    with sessions() as session:
        before = {
            row.id: row.projected
            for row in session.scalars(select(CatalogDocumentRevision))
        }
    entities.refresh_build_policy()
    with sessions() as session:
        assert before == {
            row.id: row.projected
            for row in session.scalars(select(CatalogDocumentRevision))
        }


def test_a_3_0_sync_over_a_stored_2x_row_adds_one_head_and_one_update_notice(
    sessions,
) -> None:
    """The first v3 sync: one new revision per document, the old bytes intact."""
    legacy_model, legacy_recipe = _legacy_model(), _legacy_recipe()
    model = _example("model-definition.json")
    recipe = _example("recipe-source-build.json")
    legacy_recipe["models"][0]["model"]["content_sha256"] = document_sha256(
        legacy_model
    )
    recipe["models"][0]["model"]["content_sha256"] = document_sha256(model)
    _store(
        sessions,
        legacy_model,
        "model",
        projected=_legacy_projection(legacy_model, "model"),
    )
    old_id = _store(
        sessions,
        legacy_recipe,
        "recipe",
        projected=_legacy_projection(legacy_recipe, "recipe"),
    )
    service = CatalogService(
        sessions, clock=lambda: NOW, cursors=CursorCodec(b"c" * 32)
    )

    service.import_recipe_library(
        "test",
        library_commit="a" * 40,
        source_path="recipe.json",
        document=recipe,
        expected_content_sha256=document_sha256(recipe),
        dependency_documents=[model],
    )

    with sessions() as session:
        rows = session.scalars(
            select(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.kind == "recipe")
            .order_by(CatalogDocumentRevision.revision_number)
        ).all()
        assert len(rows) == 2
        old, new = rows
        assert old.id == old_id
        assert old.document == legacy_recipe
        assert old.content_digest == document_sha256(legacy_recipe)
        assert new.content_digest == document_sha256(recipe)
        assert new.document_id == old.document_id
        head = session.scalar(
            select(CatalogDocumentHead.active_revision_id).where(
                CatalogDocumentHead.kind == "recipe"
            )
        )
        assert head == new.id
        newest = newest_active_revisions(session, {new.document_id})[new.document_id]
        assert newest.id == new.id
        notice = recipe_update_notice("Synthetic Tiny build", old, newest)
        assert notice is not None and notice.newest_revision_id == new.id
