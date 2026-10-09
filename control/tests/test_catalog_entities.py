from __future__ import annotations

import copy
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib import resources

import pytest
from sqlalchemy import create_engine, delete, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.auth import CursorCodec
from vonk_control.catalog_entities import (
    CatalogEntityService,
)
from vonk_control.catalog_revision_contract import (
    RecipeRevisionProjection,
    read_catalog_projection,
)
from vonk_control.catalog_service import CatalogService
from vonk_control.library_projection import LibraryProjection
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _example(name: str) -> dict[str, object]:
    path = resources.files("vonk_forge_contracts").joinpath("examples", name)
    return json.loads(path.read_text(encoding="utf-8"))


def _model() -> dict[str, object]:
    """Return the canonical model fixture as the mutable document it publishes."""

    document = _example("model-definition.json")
    ModelDefinition.model_validate(document)
    return document


def _recipe(
    model: dict[str, object], *, slug: str = "synthetic-tiny-build"
) -> dict[str, object]:
    """Return a validated canonical recipe document bound to *model*."""

    recipe = _example("recipe-source-build.json")
    identity = recipe["identity"]
    assert isinstance(identity, dict)
    identity["slug"] = slug
    _model_reference(recipe)["content_sha256"] = document_sha256(model)
    RecipeDefinition.model_validate(recipe)
    return recipe


def _metadata(document: dict[str, object]) -> dict[str, object]:
    metadata = document["metadata"]
    assert isinstance(metadata, dict)
    return metadata


def _license(document: dict[str, object]) -> dict[str, object]:
    license_document = document["license"]
    assert isinstance(license_document, dict)
    return license_document


def _model_reference(recipe: dict[str, object]) -> dict[str, object]:
    models = recipe["models"]
    assert isinstance(models, list) and models
    selection = models[0]
    assert isinstance(selection, dict)
    reference = selection["model"]
    assert isinstance(reference, dict)
    return reference


@pytest.fixture
def session() -> Iterator[Session]:
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

    metadata = active.document["metadata"]
    assert isinstance(metadata, dict)
    metadata["description"] = "tampered"

    with pytest.raises(ValueError, match="immutable"):
        session.commit()


def test_exact_model_reference_does_not_fall_back_to_a_newer_digest(
    service: CatalogEntityService,
) -> None:
    original = _model()
    first = _resolve(service, original)
    changed = copy.deepcopy(original)
    _metadata(changed)["description"] = "updated capability documentation"
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
    unavailable = None
    try:
        unavailable = service.resolve_reference(reference)
    except Exception:  # noqa: BLE001 - exact prior references remain usable
        assert service.get_entity(first.document_id).id == successor.id
    assert unavailable is None
    reference.content_sha256 = first.content_digest
    assert service.resolve_reference(reference).id == first.id
    reference.content_sha256 = successor.content_digest
    assert service.resolve_reference(reference).id == successor.id


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
    service: CatalogEntityService, session: Session, mutation: str
) -> None:
    model = _model()
    model_revision = _resolve(service, model)
    recipe = _recipe(model, slug=f"synthetic-tiny-{mutation.replace('_', '-')}")
    reference = _model_reference(recipe)
    if mutation == "wrong_digest":
        reference["content_sha256"] = "f" * 64
    else:
        reference["slug"] = "missing-model"
    candidate = service.create_draft(recipe, actor="operator")

    try:
        service.resolve(candidate.id, actor="operator")
    except Exception:  # noqa: BLE001 - assert no binding and fresh admission
        assert service.get_entity(model_revision.document_id).id == model_revision.id
    assert (
        session.scalar(
            select(CatalogRecipeModelReference).where(
                CatalogRecipeModelReference.recipe_revision_id == candidate.id
            )
        )
        is None
    )
    assert service.get_entity(model_revision.document_id).id == model_revision.id
    head = session.scalar(
        select(CatalogDocumentHead).where(
            CatalogDocumentHead.publisher == candidate.publisher,
            CatalogDocumentHead.slug == candidate.slug,
            CatalogDocumentHead.kind == candidate.kind,
        )
    )
    assert head is not None and head.active_revision_id is None
    service.fail_candidate(candidate.document_id)
    successor = service.revise(
        candidate.document_id,
        _recipe(model, slug=str(RecipeDefinition.model_validate(recipe).identity.slug)),
        actor="operator",
    )
    assert service.resolve(successor.id, actor="operator").id == successor.id


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
    _metadata(bad)["title"] = "candidate that fails"
    _model_reference(bad)["content_sha256"] = "f" * 64
    failed = service.revise(
        first.document_id, bad, actor="operator", expected_revision=1
    )
    try:
        service.resolve(failed.id, actor="operator")
    except Exception:  # noqa: BLE001 - retained head and next activation are the outcome
        assert service.get_entity(first.document_id).id == first.id
    assert service.get_entity(first.document_id).id == first.id
    service.fail_candidate(first.document_id, reason="model digest rejected")
    assert service.get_entity(first.document_id).id == first.id

    good = copy.deepcopy(recipe)
    _metadata(good)["title"] = "accepted successor"
    successor = service.revise(
        first.document_id, good, actor="operator", expected_revision=2
    )
    active = service.resolve(successor.id, actor="operator", expected_revision=3)
    assert active.id == successor.id
    assert service.get_entity(first.document_id).id == successor.id
    assert (
        session.scalar(
            select(CatalogRecipeModelReference).where(
                CatalogRecipeModelReference.recipe_revision_id == successor.id
            )
        )
        is not None
    )


def test_model_capability_revision_reuses_its_artifact_projection(
    service: CatalogEntityService,
) -> None:
    original = _model()
    first = _resolve(service, original)
    changed = copy.deepcopy(original)
    _metadata(changed)["description"] = "updated capability documentation"
    _license(changed)["attribution"] = ["updated attribution"]
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
    _metadata(changed_model)["description"] = "updated capability documentation"
    _license(changed_model)["attribution"] = ["updated attribution"]
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
    _metadata(changed_recipe)["title"] = "successor recipe"
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
    _metadata(changed)["description"] = "updated capability documentation"
    successor = service.revise(
        draft.document_id,
        changed,
        actor="operator",
        expected_revision=draft.revision_number,
    )

    try:
        service.resolve(
            draft.document_id,
            actor="operator",
            expected_revision=draft.revision_number,
        )
    except Exception:  # noqa: BLE001 - exact head preservation and fresh acceptance decide
        assert service.get_entity(draft.document_id).id == draft.id
    assert service.get_entity(draft.document_id).id == draft.id

    accepted = service.resolve(
        successor.id, actor="operator", expected_revision=successor.revision_number
    )
    assert service.get_entity(draft.document_id).id == accepted.id


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


# A projection as the release before the 2.0.0 recipe contract wrote it: the old
# topology shape and the retired test_report, plus the publication fields the
# sync recorded beside it.
_SOURCE_BUNDLE = "b" * 64


def _pre_contract_projection(valid: dict[str, object]) -> dict[str, object]:
    projected = copy.deepcopy(valid)
    topology = projected["topology"]
    assert isinstance(topology, dict)
    topology["mode"] = "single"
    topology["fabric"] = {"connectivity": "none", "minimum_bandwidth_mbps": 0}
    projected["test_report"] = {"result": "passed"}
    del projected["artifact_inputs"]  # derived again from the model bindings
    projected["source_bundle_sha256"] = _SOURCE_BUNDLE
    projected["publication_commit"] = "a" * 40
    # Itself invalid under the current contract: dropped, not carried over.
    projected["package_handle"] = {"archive_path": 3}
    return projected


def _store_projection(
    session: Session, revision: CatalogDocumentRevision, projected: object
) -> None:
    session.execute(
        update(CatalogDocumentRevision)
        .where(CatalogDocumentRevision.id == revision.id)
        .values(projected=projected)
    )
    session.expire(revision, ["projected"])


def _recipe_revision(
    service: CatalogEntityService,
) -> CatalogDocumentRevision:
    _resolve(service, _model())
    return _resolve(service, _recipe(_model()))


def test_refresh_rederives_a_projection_an_earlier_release_wrote(
    session: Session, service: CatalogEntityService
) -> None:
    revision = _recipe_revision(service)
    valid = dict(revision.projected)
    _store_projection(session, revision, _pre_contract_projection(valid))
    readable = read_catalog_projection(revision)
    assert isinstance(readable, RecipeRevisionProjection)
    assert readable.title == valid["title"]

    service.refresh_build_policy()

    session.refresh(revision)
    healed = read_catalog_projection(revision).model_dump(
        mode="json", exclude_none=True
    )
    assert healed == {
        **valid,
        "source_bundle_sha256": _SOURCE_BUNDLE,
        "publication_commit": "a" * 40,
    }
    again = dict(revision.projected)
    service.refresh_build_policy()
    session.refresh(revision)
    assert revision.projected == again


@pytest.mark.usefixtures("damaged_json_rows")
def test_refresh_does_not_repair_a_projection_of_a_document_that_does_not_match(
    session: Session,
    service: CatalogEntityService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    revision = _recipe_revision(service)
    broken = _pre_contract_projection(dict(revision.projected))
    _store_projection(session, revision, broken)
    session.execute(
        update(CatalogDocumentRevision)
        .where(CatalogDocumentRevision.id == revision.id)
        .values(content_digest="c" * 64)
    )
    session.expire(revision)

    with caplog.at_level(logging.WARNING):
        service.refresh_build_policy()

    revision = session.get(CatalogDocumentRevision, revision.id)
    assert revision is not None and revision.projected == broken
    identity = (revision.publisher, revision.slug)
    session.expunge_all()
    session.commit()
    sessions = sessionmaker(session.get_bind(), expire_on_commit=False)
    catalog = CatalogService(
        sessions, clock=lambda: NOW, cursors=CursorCodec(b"c" * 32)
    )
    assert catalog.recipe_catalog_local_revisions([identity]) == {}
    document = _recipe(_model())

    def ingest():
        return catalog.import_recipe_library(
            "operator",
            library_commit="a" * 40,
            source_path="recipe.json",
            document=document,
            expected_content_sha256=document_sha256(document),
            dependency_documents=[_model()],
        )

    repaired = ingest()
    assert ingest().recipe_id == repaired.recipe_id
    library = LibraryProjection(sessions, cursors=CursorCodec(b"c" * 32))
    detail = library.authoring_recipe_detail(repaired.recipe_id)
    assert detail.definition == RecipeDefinition.model_validate(document)
    assert detail.model_documents[0].model_document == ModelDefinition.model_validate(
        _model()
    )
    assert catalog.recipe_catalog_local_revisions([identity])[
        identity
    ].content_sha256 == document_sha256(document)


def test_refresh_leaves_a_superseded_old_contract_revision_alone_and_quiet(
    session: Session,
    service: CatalogEntityService,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _recipe_revision(service)
    # The document predates the 2.0.0 contract, so it cannot be read at all.
    # Bulk SQL stands in for the row an earlier release left behind.
    old = {**first.document, "topology": {"mode": "single"}}
    broken = _pre_contract_projection(dict(first.projected))
    session.execute(
        update(CatalogDocumentRevision)
        .where(CatalogDocumentRevision.id == first.id)
        .values(document=old, projected=broken)
    )
    session.expire(first)
    changed = _recipe(_model())
    _metadata(changed)["description"] = "a newer revision"
    successor = service.revise(first.document_id, changed, actor="operator")
    service.resolve(successor.id, actor="operator")

    with caplog.at_level(logging.INFO):
        service.refresh_build_policy()

    first = session.get(CatalogDocumentRevision, first.id)
    assert first is not None and first.projected == broken
    assert first.id not in caplog.text


def test_new_authorized_revision_supersedes_pending_candidate(
    session: Session,
    service: CatalogEntityService,
) -> None:
    document = _model()
    active = _resolve(service, document)
    pending_document = copy.deepcopy(document)
    _metadata(pending_document)["description"] = "older pending change"
    pending = service.revise(
        active.document_id, pending_document, actor="operator", expected_revision=1
    )
    latest_document = copy.deepcopy(document)
    _metadata(latest_document)["description"] = "new authorized change"
    latest = service.revise(
        active.document_id, latest_document, actor="operator", expected_revision=2
    )
    assert service.get_entity(active.document_id).id == active.id
    accepted = service.resolve(latest.id, actor="operator", expected_revision=3)
    assert accepted.id == latest.id
    assert accepted.id != pending.id


def test_new_revision_repairs_a_missing_head_without_guessing_history(
    session: Session,
    service: CatalogEntityService,
) -> None:
    draft = service.create_draft(_model(), actor="operator")
    session.execute(delete(CatalogDocumentHead))
    session.flush()
    changed = copy.deepcopy(draft.document)
    _metadata(changed)["description"] = "fresh accepted candidate"
    successor = service.revise(draft.document_id, changed, actor="operator")
    head = session.scalar(select(CatalogDocumentHead))
    assert head is not None
    assert head.candidate_revision_id == successor.id
    assert head.active_revision_id is None
    assert service.resolve(successor.id, actor="operator").id == successor.id


def test_new_revision_repairs_an_empty_document_root(
    session: Session,
    service: CatalogEntityService,
) -> None:
    document = _model()
    identity = ModelDefinition.model_validate(document).identity
    root = CatalogDocument(
        kind=document["kind"],
        publisher=identity.publisher,
        slug=identity.slug,
        title="empty root",
        created_by="operator",
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(root)
    session.flush()
    candidate = service.revise(root.id, document, actor="operator")
    assert candidate.revision_number == 1
    assert service.resolve(candidate.id, actor="operator").id == candidate.id


def test_recipe_resolution_rederives_missing_model_artifact_key(
    session: Session,
    service: CatalogEntityService,
) -> None:
    model = _model()
    active_model = _resolve(service, model)
    artifact_key = active_model.artifact_key
    session.execute(
        update(CatalogDocumentRevision)
        .where(CatalogDocumentRevision.id == active_model.id)
        .values(artifact_key=None)
    )
    session.expire(active_model)
    active_recipe = _resolve(service, _recipe(model))
    assert active_recipe.artifact_key is not None
    assert active_model.artifact_key == artifact_key


def test_imported_recipe_recreates_missing_lookup_root(
    session: Session,
    service: CatalogEntityService,
) -> None:
    from vonk_control.catalog_service import CatalogService

    model = _model()
    _resolve(service, model)
    revision = _resolve(service, _recipe(model))
    root_id = revision.document_id
    session.execute(delete(CatalogDocument).where(CatalogDocument.id == root_id))
    catalog = CatalogService(
        sessionmaker(bind=session.get_bind()),
        clock=lambda: NOW,
        cursors=CursorCodec(b"x" * 32),
    )
    catalog._select_imported_recipe_head(session, revision)
    session.flush()
    root = session.get(CatalogDocument, root_id)
    assert root is not None
    assert (root.publisher, root.slug) == (revision.publisher, revision.slug)


def test_explicit_activation_restores_only_the_named_missing_selection(
    session: Session,
    service: CatalogEntityService,
) -> None:
    revision = _resolve(service, _model())
    session.execute(delete(CatalogDocumentHead))
    assert service.resolve(revision.id, actor="operator").id == revision.id
    head = session.scalar(select(CatalogDocumentHead))
    assert head is not None and head.active_revision_id == revision.id
