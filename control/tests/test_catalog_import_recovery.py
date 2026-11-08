"""Catalog maintenance damage cannot own import admission or erase verified pins."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any, cast

import pytest
import vonk_control.catalog_entities as entities
from sqlalchemy import create_engine, delete, event, select, update
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.auth import CursorCodec
from vonk_control.catalog_revision_contract import (
    PrebuiltImage,
    RecipeRevisionProjection,
    read_catalog_projection,
)
from vonk_control.catalog_service import CatalogService, RecipeRevisionView
from vonk_control.catalog_sync import _controller_marker as controller_marker
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    RecipeInstallation,
    RecipeRun,
)
from vonk_forge_contracts import RecipeApi, RecipeDefinition, document_sha256

from .display_fixtures import reword
from .test_catalog_entities import (
    _example,
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


def _request_import(catalog: CatalogService, document) -> RecipeRevisionView:
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
    first = _request_import(catalog, _recipe(_model()))
    changed = _recipe(_model())
    reword(changed, "older pending content")
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
        reword(document, description)
        imported = _request_import(catalog, document)
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
    first = _request_import(catalog, _recipe(_model()))
    changed = _recipe(_model())
    reword(changed, "pending")
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
    accepted = _request_import(catalog, changed)
    assert _request_import(catalog, changed).id == accepted.id


def test_policy_fault_preserves_one_projection_and_repairs_healthy_siblings(
    catalog: CatalogService, monkeypatch
):
    documents = [
        _recipe(_model(), slug=slug) for slug in ("policy-broken", "policy-healthy")
    ]
    views = [_request_import(catalog, document) for document in documents]
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
        _request_import(catalog, _recipe(_model(), slug="fresh-during-policy-fault"))
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
    assert _request_import(catalog, documents[0]).id == views[0].id


def test_projection_write_fault_rolls_back_only_its_row(
    catalog: CatalogService, monkeypatch
):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    views = [
        _request_import(catalog, _recipe(_model(), slug=slug))
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
    assert _request_import(
        catalog, _recipe(_model(), slug="fresh-after-write-fault")
    ).id


def test_partial_pin_observation_preserves_omitted_revisions(catalog: CatalogService):
    views = [
        _request_import(catalog, _recipe(_model(), slug=slug))
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
    catalog.record_published_facts(dict.fromkeys(keys, image), {})
    catalog.record_published_facts({keys[0]: image}, {})
    with catalog._sessions() as session:
        assert all(
            _projection(session, view.id).prebuilt_image == image for view in views
        )
    # An explicit authoritative withdrawal affects only its exact revision.
    catalog.record_published_facts({keys[0]: None}, {})
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
    catalog.record_published_facts({keys[0]: image}, {})
    assert _request_import(
        catalog, _recipe(_model(), slug="fresh-after-pin-withdrawal")
    ).id


def test_published_api_facts_belong_to_their_exact_revision(catalog: CatalogService):
    views = [
        _request_import(catalog, _recipe(_model(), slug=slug))
        for slug in ("api-one", "api-two")
    ]
    keys = [
        (
            str(RecipeDefinition.model_validate(view.document).identity.publisher),
            view.slug,
            str(view.content_sha256),
        )
        for view in views
    ]
    api = RecipeApi.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-api-decision.json")
            .read_text(encoding="utf-8")
        )
    )

    def stored() -> list[RecipeApi | None]:
        with catalog._sessions() as session:
            return [_projection(session, view.id).api for view in views]

    catalog.record_published_facts({}, {}, dict.fromkeys(keys, api))
    assert stored() == [api, api]
    # An omitted key is unknown and keeps its facts; an observed None withdraws
    # them from that exact revision only.
    catalog.record_published_facts({}, {}, {keys[0]: None})
    assert stored() == [None, api]
    catalog.record_published_facts({}, {})
    assert stored() == [None, api]


def test_a_stored_api_without_an_effort_kind_is_dropped_and_recorded_again(
    catalog: CatalogService,
):
    """The upgrade to contracts with effort kinds heals by itself on the next sync.

    A projection stored before the union holds ``api`` with an effort that has no
    ``kind``. Reading it drops that one fact (the rest of the projection stays);
    the next sync records the API again from the signed index. The contracts
    version is part of the Controller marker, so that sync runs after the upgrade
    without anyone acting.
    """
    view = _request_import(catalog, _recipe(_model(), slug="api-kindless"))
    key = (
        str(RecipeDefinition.model_validate(view.document).identity.publisher),
        view.slug,
        str(view.content_sha256),
    )
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-api-options.json")
        .read_text(encoding="utf-8")
    )
    document["api"]["reasoning"] = {
        "kind": "always-on",
        "effort": {
            "kind": "text",
            "via": "top_level",
            "key": "reasoning_effort",
            "default": None,
        },
    }
    api = RecipeApi.model_validate(document)
    catalog.record_published_facts({}, {}, {key: api})

    def stored_effort() -> dict[str, object]:
        with catalog._sessions() as session:
            row = session.get(CatalogDocumentRevision, view.id)
            assert row is not None
            return cast(Any, row.projected)["api"]["api"]["reasoning"]["effort"]

    assert stored_effort()["kind"] == "text"
    # What the previous release stored: the same facts, the effort without a kind.
    with catalog._sessions.begin() as session:
        row = session.get(CatalogDocumentRevision, view.id)
        assert row is not None
        projected = cast(Any, deepcopy(row.projected))
        del projected["api"]["api"]["reasoning"]["effort"]["kind"]
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == view.id)
            .values(projected=projected)
        )
    with catalog._sessions() as session:
        read = _projection(session, view.id)
        assert (
            read.api is None
        )  # dropped on read; the model_info has nothing to publish
        assert read.runtime_engine  # the rest of the projection is unaffected
    # The next sync's recording restores it, and a repeat changes nothing.
    catalog.record_published_facts({}, {}, {key: api})
    with catalog._sessions() as session:
        assert _projection(session, view.id).api == api
    # That sync runs after the upgrade because the marker names the contracts version.
    assert "vonk-forge-public-contracts=" in controller_marker()


def test_kit_topology_change_promotes_head_and_preserves_exact_prior_revision(
    catalog: CatalogService,
):
    first = _request_import(catalog, _recipe(_model()))
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
    second = _request_import(catalog, dual)
    assert catalog.get_recipe(first.recipe_id).id == second.id
    assert catalog.get_recipe(first.id).document == first.document
    assert RecipeDefinition.model_validate(second.document).topology.node_count == 2
    assert _request_import(catalog, dual).id == second.id
    with catalog._sessions() as session:
        run = session.get(RecipeRun, run_id)
        installation = session.get(RecipeInstallation, installation_id)
        assert run is not None and installation is not None
        assert run.plan == prior_plan
        assert installation.recipe_revision_id == first.id


def test_current_catalog_maintenance_writes_nothing(catalog: CatalogService):
    """Every sync re-checks the catalog; a current one is left untouched."""
    view = _request_import(catalog, _recipe(_model()))
    assert view.content_sha256 is not None
    pins = {
        (
            str(RecipeDefinition.model_validate(view.document).identity.publisher),
            view.slug,
            view.content_sha256,
        ): PrebuiltImage(
            reference="ghcr.io/vonk/image@sha256:" + "1" * 64, build_key="2" * 64
        )
    }
    catalog.refresh_build_policy()
    catalog.record_published_facts(pins, {})
    engine = catalog._sessions.kw["bind"]
    writes: list[str] = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert catalog.refresh_build_policy() == ()
        catalog.record_published_facts(pins, {})
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert writes == []
