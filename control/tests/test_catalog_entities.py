from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from importlib import resources

import pytest
from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
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
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _example(name: str) -> dict[str, object]:
    path = resources.files("vonk_forge_contracts").joinpath("examples", name)
    return json.loads(path.read_text(encoding="utf-8"))


def _model() -> dict[str, object]:
    return _example("model-definition.json")


def _recipe(
    model: dict[str, object], *, slug: str = "synthetic-tiny-image"
) -> dict[str, object]:
    recipe = _example("recipe-image.json")
    recipe["identity"]["slug"] = slug
    recipe["models"][0]["model"]["content_sha256"] = content_sha256(
        ModelDefinition.model_validate(model)
    )
    return recipe


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        yield value


@pytest.fixture
def service(session: Session) -> CatalogEntityService:
    return CatalogEntityService(session, clock=lambda: NOW)


def _resolve(
    service: CatalogEntityService, document: dict[str, object]
) -> CatalogDocumentRevision:
    draft = service.create_draft(document, actor="operator")
    return service.resolve(draft.id, actor="operator")


def test_active_canonical_revision_is_immutable(
    session: Session, service: CatalogEntityService
) -> None:
    active = _resolve(service, _model())

    active.document["metadata"]["description"] = "tampered"

    with pytest.raises(ValueError, match="immutable"):
        session.commit()


def test_exact_model_reference_does_not_fall_back_to_a_newer_digest(
    service: CatalogEntityService,
) -> None:
    original = _model()
    first = _resolve(service, original)
    changed = copy.deepcopy(original)
    changed["metadata"]["description"] = "updated capability documentation"
    successor = service.revise(first.document_id, changed, actor="operator")
    successor = service.resolve(successor.id, actor="operator")

    recipe = RecipeDefinition.model_validate(_recipe(original))
    reference = recipe.models[0].model
    reference.slug = first.slug
    reference.content_sha256 = first.content_digest
    assert service.resolve_reference(reference).id == first.id
    reference.content_sha256 = successor.content_digest
    assert service.resolve_reference(reference).id == successor.id
    reference.content_sha256 = "f" * 64
    with pytest.raises(CatalogValidationError, match="exact referenced document"):
        service.resolve_reference(reference)


def test_resolving_the_same_canonical_draft_is_idempotent(
    service: CatalogEntityService,
) -> None:
    draft = service.create_draft(_model(), actor="operator")

    active = service.resolve(draft.id, actor="operator")
    repeated = service.resolve(draft.id, actor="operator")

    assert repeated.id == active.id
    assert repeated.content_digest == active.content_digest


@pytest.mark.parametrize("mutation", ["wrong_digest", "missing_model"])
def test_recipe_resolution_requires_an_exact_active_model_revision(
    service: CatalogEntityService, mutation: str
) -> None:
    model = _model()
    model_revision = _resolve(service, model)
    recipe = _recipe(model, slug=f"synthetic-tiny-{mutation.replace('_', '-')}")
    if mutation == "wrong_digest":
        recipe["models"][0]["model"]["content_sha256"] = "f" * 64
    else:
        recipe["models"][0]["model"]["slug"] = "missing-model"
    candidate = service.create_draft(recipe, actor="operator")

    with pytest.raises(CatalogValidationError, match="model reference"):
        service.resolve(candidate.id, actor="operator")

    assert model_revision.state == "active"


def test_recipe_resolution_records_exact_model_revision_binding(
    session: Session, service: CatalogEntityService
) -> None:
    model = _model()
    model_revision = _resolve(service, model)
    recipe_revision = _resolve(service, _recipe(model))

    binding = session.scalar(
        select(CatalogRecipeModelReference).where(
            CatalogRecipeModelReference.recipe_revision_id == recipe_revision.id
        )
    )
    assert binding is not None
    assert binding.model_revision_id == model_revision.id
    assert binding.model_content_digest == model_revision.content_digest


def test_failed_recipe_candidate_preserves_the_prior_active_revision(
    session: Session, service: CatalogEntityService
) -> None:
    model = _model()
    _resolve(service, model)
    recipe = _recipe(model)
    first = _resolve(service, recipe)

    bad = copy.deepcopy(recipe)
    bad["metadata"]["title"] = "candidate that fails"
    bad["models"][0]["model"]["content_sha256"] = "f" * 64
    failed = service.revise(
        first.document_id, bad, actor="operator", expected_revision=1
    )
    with pytest.raises(CatalogValidationError):
        service.resolve(failed.id, actor="operator")
    service.fail_candidate(first.document_id, reason="model digest rejected")
    assert service.get_entity(first.document_id).id == first.id

    good = copy.deepcopy(recipe)
    good["metadata"]["title"] = "accepted successor"
    successor = service.revise(
        first.document_id, good, actor="operator", expected_revision=2
    )
    active = service.resolve(successor.id, actor="operator", expected_revision=3)
    assert active.id == successor.id
    assert service.get_entity(first.document_id).id == successor.id
    assert session.scalar(
        select(CatalogRecipeModelReference).where(
            CatalogRecipeModelReference.recipe_revision_id == successor.id
        )
    ) is not None


def test_model_capability_revision_reuses_its_artifact_projection(
    service: CatalogEntityService,
) -> None:
    original = _model()
    first = _resolve(service, original)
    changed = copy.deepcopy(original)
    changed["metadata"]["description"] = "updated capability documentation"
    changed["provenance"]["evidence_digest"] = "a" * 64
    successor = service.revise(
        first.document_id, changed, actor="operator", expected_revision=1
    )
    successor = service.resolve(successor.id, actor="operator", expected_revision=2)

    assert first.artifact_key == successor.artifact_key
    assert first.execution_key == successor.execution_key
    assert first.content_digest != successor.content_digest


def test_recipe_reuse_keys_follow_effective_model_artifact(
    service: CatalogEntityService,
) -> None:
    original = _model()
    model_revision = _resolve(service, original)
    first_recipe = _resolve(service, _recipe(original))

    changed_model = copy.deepcopy(original)
    changed_model["metadata"]["description"] = "updated capability documentation"
    changed_model["provenance"]["evidence_digest"] = "a" * 64
    changed_model_revision = service.revise(
        model_revision.document_id,
        changed_model,
        actor="operator",
        expected_revision=1,
    )
    changed_model_revision = service.resolve(
        changed_model_revision.id, actor="operator", expected_revision=2
    )

    changed_recipe = _recipe(changed_model, slug="synthetic-tiny-successor")
    changed_recipe["metadata"]["title"] = "successor recipe"
    successor = _resolve(service, changed_recipe)

    assert first_recipe.artifact_key == successor.artifact_key
    assert first_recipe.execution_key == successor.execution_key
    assert changed_model_revision.content_digest != model_revision.content_digest


def test_database_foreign_key_restricts_bulk_canonical_parent_deletion() -> None:
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        service = CatalogEntityService(session, clock=lambda: NOW)
        draft = service.create_draft(_model(), actor="operator")
        session.commit()

        with pytest.raises(IntegrityError):
            session.execute(
                delete(CatalogDocument).where(CatalogDocument.id == draft.document_id)
            )


def test_resolve_checks_the_expected_canonical_revision_number(
    service: CatalogEntityService,
) -> None:
    draft = service.create_draft(_model(), actor="operator")
    service.resolve(draft.id, actor="operator")
    changed = copy.deepcopy(draft.document)
    changed["metadata"]["description"] = "updated capability documentation"
    service.revise(
        draft.document_id,
        changed,
        actor="operator",
        expected_revision=draft.revision_number,
    )

    with pytest.raises(CatalogConflict) as caught:
        service.resolve(
            draft.document_id,
            actor="operator",
            expected_revision=draft.revision_number,
        )

    assert caught.value.code == "catalog.stale_revision"


def test_canonical_head_tracks_the_active_revision(
    session: Session, service: CatalogEntityService
) -> None:
    active = _resolve(service, _model())

    head = session.scalar(
        select(CatalogDocumentHead).where(
            CatalogDocumentHead.kind == active.kind,
            CatalogDocumentHead.publisher == active.publisher,
            CatalogDocumentHead.slug == active.slug,
        )
    )
    assert head is not None
    assert head.active_revision_id == active.id
    assert head.candidate_revision_id is None
