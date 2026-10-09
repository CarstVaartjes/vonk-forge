"""Catalog maintenance damage cannot own import admission or erase verified pins."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
import vonk_control.catalog_entities as entities
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.auth import CursorCodec
from vonk_control.catalog_revision_contract import (
    PrebuiltImage,
    RecipeRevisionProjection,
    read_catalog_projection,
)
from vonk_control.catalog_service import CatalogService, RecipeRevisionView
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    RecipeInstallation,
    RecipeRun,
)
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .test_catalog_entities import (
    _example,
    _metadata,
    _model,
    _model_reference,
    _recipe,
)
from .test_catalog_revision_collection import Catalog

NOW = datetime(2026, 10, 9, tzinfo=UTC)


@pytest.fixture
def catalog() -> CatalogService:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return CatalogService(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: NOW,
        cursors=CursorCodec(b"c" * 32),
    )


def _import(catalog: CatalogService, document) -> RecipeRevisionView:
    return catalog.import_recipe_library(
        "test",
        library_commit="a" * 40,
        source_path="recipe.json",
        document=document,
        expected_content_sha256=document_sha256(document),
        dependency_documents=[_model()],
    )


def _projection(session: Session, identity: str) -> RecipeRevisionProjection:
    row = session.get(CatalogDocumentRevision, identity)
    assert row is not None
    projection = read_catalog_projection(row)
    assert isinstance(projection, RecipeRevisionProjection)
    return projection


def _cores(session: Session, identity: str) -> int:
    resources = _projection(session, identity).build_resources
    assert resources is not None
    return resources.cpu_cores


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("damage", [[], {}, "unreadable", {"title": 7}])
@pytest.mark.parametrize("missing_root", [False, True])
def test_poisoned_candidate_releases_head_and_accepts_two_fresh_imports(
    catalog: CatalogService, damage, missing_root
):
    first = _import(catalog, _recipe(_model()))
    changed = _recipe(_model())
    _metadata(changed)["description"] = "older pending content"
    pending = catalog.entities.revise(first.recipe_id, changed, actor="test")
    with catalog._sessions.begin() as session:
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == pending.id)
            .values(projected=damage, document={"unreadable": True})
        )
        if missing_root:
            session.execute(
                delete(CatalogDocument).where(CatalogDocument.id == first.recipe_id)
            )
    for description in ("new accepted content", "newer accepted content"):
        document = _recipe(_model())
        _metadata(document)["description"] = description
        imported = _import(catalog, document)
        with catalog._sessions() as session:
            head = session.scalar(
                select(CatalogDocumentHead).where(
                    CatalogDocumentHead.kind
                    == RecipeDefinition.model_validate(imported.document).kind,
                    CatalogDocumentHead.slug == imported.slug,
                )
            )
            assert head is not None and head.candidate_revision_id is None
            assert head.active_revision_id == imported.id
        assert (
            catalog.get_recipe(first.recipe_id).content_sha256
            == imported.content_sha256
        )
    # Supersession preserves the exact immutable historical recipe.
    assert catalog.get_recipe(first.id).content_sha256 == first.content_sha256


@pytest.mark.parametrize("missing_root", [False, True])
def test_history_projection_failure_cannot_hold_candidate_ownership(
    catalog: CatalogService, monkeypatch, missing_root
):
    first = _import(catalog, _recipe(_model()))
    changed = _recipe(_model())
    _metadata(changed)["description"] = "pending"
    catalog.entities.revise(first.recipe_id, changed, actor="test")
    if missing_root:
        with catalog._sessions.begin() as session:
            session.execute(
                delete(CatalogDocument).where(CatalogDocument.id == first.recipe_id)
            )
    real_read = entities.read_catalog_projection

    def unavailable_history(_revision):
        raise OSError("history store unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(entities, "read_catalog_projection", unavailable_history)
        catalog.entities.fail_candidate(first.recipe_id, reason="superseded")
    monkeypatch.setattr(entities, "read_catalog_projection", real_read)
    accepted = _import(catalog, changed)
    assert _import(catalog, changed).id == accepted.id


def test_policy_fault_preserves_one_projection_and_repairs_healthy_siblings(
    catalog: CatalogService, monkeypatch
):
    documents = [
        _recipe(_model(), slug=slug) for slug in ("policy-broken", "policy-healthy")
    ]
    views = [_import(catalog, document) for document in documents]
    with catalog._sessions.begin() as session:
        for view in views:
            row = session.get(CatalogDocumentRevision, view.id)
            assert row is not None
            projection = read_catalog_projection(row)
            assert isinstance(projection, RecipeRevisionProjection)
            assert projection.build_resources is not None
            resources = projection.build_resources.model_copy(update={"cpu_cores": 1})
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == row.id)
                .values(
                    projected=projection.model_copy(
                        update={"build_resources": resources}
                    ).model_dump(mode="json")
                )
            )
    compile_policy = entities.build_policy_projection
    failed_calls = 0

    def compile_with_fault(recipe):
        nonlocal failed_calls
        if recipe.identity.slug == "policy-broken":
            failed_calls += 1
            raise OSError("compiler dependency unavailable")
        return compile_policy(recipe)

    with monkeypatch.context() as patch:
        patch.setattr(entities, "build_policy_projection", compile_with_fault)
        deferred = catalog.refresh_build_policy()
        assert len(deferred) == 1
        assert views[0].content_sha256 is not None
        assert views[0].content_sha256 in str(deferred[0].recipe_uri)
        # A fresh request runs through the normal importer despite the old miss.
        _import(catalog, _recipe(_model(), slug="fresh-during-policy-fault"))
    assert failed_calls == 1
    with catalog._sessions() as session:
        broken, healthy = [
            session.get(CatalogDocumentRevision, view.id) for view in views
        ]
        assert broken is not None and healthy is not None
        assert _cores(session, broken.id) == 1
        assert _cores(session, healthy.id) == 8
    assert catalog.refresh_build_policy() == ()
    with catalog._sessions() as session:
        repaired = session.get(CatalogDocumentRevision, views[0].id)
        assert repaired is not None and _cores(session, repaired.id) == 8
    assert _import(catalog, documents[0]).id == views[0].id


def test_projection_write_fault_rolls_back_only_its_savepoint(
    catalog: CatalogService, monkeypatch
):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    views = [
        _import(catalog, _recipe(_model(), slug=slug))
        for slug in ("write-broken", "write-healthy")
    ]
    # Force recompilation without altering accepted source bytes.
    with catalog._sessions.begin() as session:
        for view in views:
            row = session.get(CatalogDocumentRevision, view.id)
            assert row is not None
            projection = read_catalog_projection(row)
            assert isinstance(projection, RecipeRevisionProjection)
            assert projection.build_resources is not None
            resources = projection.build_resources.model_copy(update={"cpu_cores": 1})
            session.execute(
                update(CatalogDocumentRevision)
                .where(CatalogDocumentRevision.id == row.id)
                .values(
                    projected=projection.model_copy(
                        update={"build_resources": resources}
                    ).model_dump(mode="json")
                )
            )
    engine = catalog._sessions.kw["bind"]

    def fail_one_write(_connection, _cursor, statement, parameters, _context, _many):
        if (
            statement.startswith("UPDATE catalog_document_revisions")
            and views[0].id in parameters
        ):
            raise OperationalError(
                statement, parameters, OSError("projection write unavailable")
            )

    event.listen(engine, "before_cursor_execute", fail_one_write)
    try:
        catalog.refresh_build_policy()
    finally:
        event.remove(engine, "before_cursor_execute", fail_one_write)
    with catalog._sessions() as session:
        for index, view in enumerate(views):
            row = session.get(CatalogDocumentRevision, view.id)
            assert row is not None
            assert _cores(session, row.id) == (1 if index == 0 else 8)
    catalog.refresh_build_policy()
    with catalog._sessions() as session:
        assert _cores(session, views[0].id) == 8
    assert _import(catalog, _recipe(_model(), slug="fresh-after-write-fault")).id


def test_partial_pin_observation_preserves_omitted_revisions(catalog: CatalogService):
    views = [
        _import(catalog, _recipe(_model(), slug=slug))
        for slug in ("pin-one", "pin-two")
    ]
    image = PrebuiltImage(
        reference="ghcr.io/vonk/image@sha256:" + "1" * 64, build_key="2" * 64
    )
    keys: list[tuple[str, str, str]] = []
    for view in views:
        assert view.content_sha256 is not None
        keys.append(
            (
                str(RecipeDefinition.model_validate(view.document).identity.publisher),
                view.slug,
                view.content_sha256,
            )
        )
    catalog.record_prebuilt_images(dict.fromkeys(keys, image))
    catalog.record_prebuilt_images({keys[0]: image})
    with catalog._sessions() as session:
        assert all(
            _projection(session, view.id).prebuilt_image == image for view in views
        )
    # An explicit authoritative withdrawal affects only its exact revision.
    catalog.record_prebuilt_images({keys[0]: None})
    with catalog._sessions() as session:
        first = _projection(session, views[0].id)
        second = _projection(session, views[1].id)
        assert (
            isinstance(first, RecipeRevisionProjection) and first.prebuilt_image is None
        )
        assert (
            isinstance(second, RecipeRevisionProjection)
            and second.prebuilt_image == image
        )
    catalog.record_prebuilt_images({keys[0]: image})
    assert _import(catalog, _recipe(_model(), slug="fresh-after-pin-withdrawal")).id


def test_kit_topology_change_promotes_head_and_preserves_exact_prior_revision(
    catalog: CatalogService,
):
    first = _import(catalog, _recipe(_model()))
    workloads = Catalog(catalog._sessions.kw["bind"])
    installation_id, run_id = workloads.workload(first.id)
    with catalog._sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        prior_plan = run.plan
    dual = deepcopy(_example("recipe-dual.json"))
    single = _recipe(_model())
    dual["identity"] = single["identity"]
    _model_reference(dual)["content_sha256"] = document_sha256(_model())
    RecipeDefinition.model_validate(dual)
    second = _import(catalog, dual)
    assert catalog.get_recipe(first.recipe_id).id == second.id
    assert catalog.get_recipe(first.id).document == first.document
    assert RecipeDefinition.model_validate(second.document).topology.node_count == 2
    assert _import(catalog, dual).id == second.id
    with catalog._sessions() as session:
        run = session.get(RecipeRun, run_id)
        installation = session.get(RecipeInstallation, installation_id)
        assert run is not None and installation is not None
        assert run.plan == prior_plan
        assert installation.recipe_revision_id == first.id
