from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from vonk_control.catalog_entities import (
    CatalogConflict,
    CatalogEntityService,
    CatalogValidationError,
)
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    ModelCacheArtifact,
)
from vonk_forge_contracts import ModelDefinition, content_sha256

NOW = datetime(2026, 9, 5, tzinfo=UTC)


def _example(name: str) -> dict[str, object]:
    path = resources.files("vonk_forge_contracts").joinpath("examples", name)
    return json.loads(path.read_text())


@pytest.fixture
def catalog(postgres_engine):
    tables = [CatalogRecipeModelReference.__table__, CatalogDocumentHead.__table__, CatalogDocumentRevision.__table__, CatalogDocument.__table__]
    Base.metadata.drop_all(postgres_engine, tables=tables)
    Base.metadata.create_all(postgres_engine, tables=[CatalogDocument.__table__, CatalogDocumentRevision.__table__, CatalogDocumentHead.__table__, CatalogRecipeModelReference.__table__])
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    try:
        yield CatalogEntityService(sessions, clock=lambda: NOW)
    finally:
        Base.metadata.drop_all(postgres_engine, tables=tables)


def _model() -> dict[str, object]:
    return _example("model-definition.json")


def _recipe(model: dict[str, object]) -> dict[str, object]:
    recipe = _example("recipe-image.json")
    recipe["models"][0]["model"]["content_sha256"] = content_sha256(ModelDefinition.model_validate(model))
    return recipe


def test_fresh_postgres_migration_builds_canonical_schema(postgres_engine) -> None:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", postgres_engine.url.render_as_string(hide_password=False))
    command.upgrade(config, "head")

    inspector = inspect(postgres_engine)
    assert {
        "catalog_documents",
        "catalog_document_revisions",
        "catalog_document_heads",
        "catalog_recipe_model_references",
    } <= set(inspector.get_table_names())
    assert {
        "catalog_entities",
        "catalog_entity_revisions",
        "recipes",
        "recipe_revisions",
    }.isdisjoint(inspector.get_table_names())
    with postgres_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0021_runtime_authz"


def test_valid_model_write_read_and_projection_is_postgres_backed(catalog) -> None:
    revision = catalog.create_draft(_model(), actor="operator")
    active = catalog.resolve(revision.id, actor="operator", expected_revision=1)
    assert active.content_digest == content_sha256(ModelDefinition.model_validate(_model()))
    assert active.download_bytes == 1024
    assert active.installed_bytes == 1024
    assert active.artifact_key
    with catalog._sessions() as session:
        stored = session.scalar(select(CatalogDocumentRevision).where(CatalogDocumentRevision.id == active.id))
        assert stored is not None
        assert stored.document["schema_version"] == 2
        assert session.scalar(select(CatalogDocumentHead).where(CatalogDocumentHead.active_revision_id == active.id)) is not None


def test_catalog_identity_is_unique_in_postgres(catalog) -> None:
    document = _model()
    catalog.create_draft(document, actor="operator")
    with pytest.raises(CatalogConflict, match="identity already exists"):
        catalog.create_draft(document, actor="operator")


def test_postgres_persists_verified_zero_byte_model_artifact(postgres_engine) -> None:
    Base.metadata.create_all(postgres_engine, tables=[ModelCacheArtifact.__table__])
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    empty_digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    try:
        with sessions.begin() as session:
            session.add(
                ModelCacheArtifact(
                    sha256=empty_digest,
                    identity={"path": "empty-support-file"},
                    storage_key="objects/empty-support-file",
                    expected_bytes=0,
                    actual_bytes=0,
                    state="verified",
                    updated_at=NOW,
                )
            )
        with sessions.begin() as session:
            session.add(
                ModelCacheArtifact(
                    sha256="f" * 64,
                    identity={"path": "invalid-empty-support-file"},
                    storage_key="objects/invalid-empty-support-file",
                    expected_bytes=0,
                    actual_bytes=0,
                    state="verified",
                    updated_at=NOW,
                )
            )
            with pytest.raises(IntegrityError):
                session.flush()
    finally:
        Base.metadata.drop_all(postgres_engine, tables=[ModelCacheArtifact.__table__])


def test_active_canonical_json_cannot_be_mutated(catalog) -> None:
    draft = catalog.create_draft(_model(), actor="operator")
    active = catalog.resolve(draft.id, actor="operator")
    with catalog._sessions() as session:
        stored = session.get(CatalogDocumentRevision, active.id)
        assert stored is not None
        stored.document["metadata"]["description"] = "tampered"
        with pytest.raises(ValueError, match="immutable"):
            session.commit()


@pytest.mark.parametrize("mutation", ["wrong_digest", "missing_model"])
def test_recipe_resolution_rejects_wrong_or_missing_exact_model_fk(catalog, mutation: str) -> None:
    model = _model()
    model_revision = catalog.create_draft(model, actor="operator")
    catalog.resolve(model_revision.id, actor="operator")
    recipe = _recipe(model)
    if mutation == "wrong_digest":
        recipe["models"][0]["model"]["content_sha256"] = "f" * 64
    else:
        recipe["models"][0]["model"]["slug"] = "missing-model"
    candidate = catalog.create_draft(recipe, actor="operator")
    with pytest.raises(CatalogValidationError, match="model reference"):
        catalog.resolve(candidate.id, actor="operator")


def test_database_rejects_recipe_reference_without_exact_model_revision(catalog) -> None:
    model = _model()
    model_revision = catalog.create_draft(model, actor="operator")
    catalog.resolve(model_revision.id, actor="operator")
    candidate = catalog.create_draft(_recipe(model), actor="operator")
    with catalog._sessions() as session:
        session.add(CatalogRecipeModelReference(
            recipe_revision_id=candidate.id,
            recipe_kind="recipe",
            selection_id="model-selection",
            model_revision_id="00000000-0000-0000-0000-000000000000",
            model_kind="model",
            model_publisher=model_revision.publisher,
            model_slug=model_revision.slug,
            model_content_digest="f" * 64,
        ))
        with pytest.raises(IntegrityError):
            session.flush()


def test_candidate_switch_is_atomic_and_failed_candidate_preserves_prior_good(catalog) -> None:
    model = _model()
    model_revision = catalog.create_draft(model, actor="operator")
    catalog.resolve(model_revision.id, actor="operator")
    recipe = _recipe(model)
    first = catalog.create_draft(recipe, actor="operator")
    catalog.resolve(first.id, actor="operator")

    bad = copy.deepcopy(recipe)
    bad["metadata"]["title"] = "candidate that fails"
    bad["models"][0]["model"]["content_sha256"] = "f" * 64
    failed = catalog.revise(first.document_id, bad, actor="operator", expected_revision=1)
    with pytest.raises(CatalogValidationError):
        catalog.resolve(failed.id, actor="operator")
    catalog.fail_candidate(first.document_id, reason="model digest rejected")
    assert catalog.get_entity(first.document_id).id == first.id

    good = copy.deepcopy(recipe)
    good["metadata"]["title"] = "accepted successor"
    successor = catalog.revise(first.document_id, good, actor="operator", expected_revision=2)
    active = catalog.resolve(successor.id, actor="operator", expected_revision=3)
    assert active.id == successor.id
    assert catalog.get_entity(first.document_id).id == successor.id
    with catalog._sessions() as session:
        assert session.scalar(select(CatalogRecipeModelReference).where(CatalogRecipeModelReference.recipe_revision_id == successor.id)) is not None


def test_capability_and_provenance_only_model_revision_reuses_artifact_key(catalog) -> None:
    original = _model()
    first = catalog.create_draft(original, actor="operator")
    catalog.resolve(first.id, actor="operator")
    changed = copy.deepcopy(original)
    changed["metadata"]["description"] = "updated capability documentation"
    changed["provenance"]["evidence_digest"] = "a" * 64
    successor = catalog.revise(first.document_id, changed, actor="operator", expected_revision=1)
    catalog.resolve(successor.id, actor="operator", expected_revision=2)
    assert first.artifact_key == successor.artifact_key
    assert first.execution_key == successor.execution_key
    assert first.content_digest != successor.content_digest


def test_recipe_reuse_keys_follow_effective_execution_and_model_artifacts(catalog) -> None:
    original = _model()
    model_revision = catalog.create_draft(original, actor="operator")
    catalog.resolve(model_revision.id, actor="operator")
    first_recipe = catalog.create_draft(_recipe(original), actor="operator")
    catalog.resolve(first_recipe.id, actor="operator")

    changed_model = copy.deepcopy(original)
    changed_model["metadata"]["description"] = "updated capability documentation"
    changed_model["provenance"]["evidence_digest"] = "a" * 64
    changed_model_revision = catalog.revise(model_revision.document_id, changed_model, actor="operator", expected_revision=1)
    catalog.resolve(changed_model_revision.id, actor="operator", expected_revision=2)

    changed_recipe = copy.deepcopy(_recipe(original))
    changed_recipe["metadata"]["title"] = "successor recipe"
    changed_recipe["models"][0]["model"]["content_sha256"] = changed_model_revision.content_digest
    successor = catalog.revise(first_recipe.document_id, changed_recipe, actor="operator", expected_revision=1)
    catalog.resolve(successor.id, actor="operator", expected_revision=2)

    assert first_recipe.artifact_key == successor.artifact_key
    assert first_recipe.execution_key == successor.execution_key
