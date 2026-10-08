"""Local projection damage never changes content trust or poisons fresh work."""

import json
from datetime import UTC, datetime
from importlib.resources import files

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AssetAvailability, LifecycleState
from vonk_control.auth import CursorCodec
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.catalog_revision_contract import (
    ModelRevisionProjection,
    read_catalog_document,
    read_catalog_projection,
)
from vonk_control.catalog_service import CatalogService
from vonk_control.library_projection import LibraryProjection
from vonk_control.models import Base, CatalogDocumentRevision
from vonk_control.operation_api.contracts import OperationListPage, OperationProvider
from vonk_control.operation_api.providers import merge_operation_providers
from vonk_control.operation_item_contract import OperationItem


@pytest.mark.usefixtures("damaged_json_rows")
def test_verified_catalog_reingestion_repairs_local_bytes_without_a_new_revision():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = CatalogService(
        sessions, clock=lambda: datetime.now(UTC), cursors=CursorCodec(b"c" * 32)
    )
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    service.import_catalog_models("test", [document])
    with sessions.begin() as session:
        row = session.scalar(select(CatalogDocumentRevision))
        assert row is not None
        identity = row.id
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == identity)
            .values(document={"broken": True}, projected={"broken": True})
        )
        session.expunge(row)
    # The new import uses verified ingress content, never the damaged local bytes.
    service.import_catalog_models("test", [document])
    with sessions() as session:
        rows = session.scalars(select(CatalogDocumentRevision)).all()
        assert len(rows) == 1 and rows[0].id == identity
        assert (
            read_catalog_document(rows[0]).identity.slug == document["identity"]["slug"]
        )
        projected = read_catalog_projection(rows[0])
        assert isinstance(projected, ModelRevisionProjection)
        assert projected.artifact_count == len(document["files"])
    service.import_catalog_models("test", [document])
    with sessions() as session:
        assert len(session.scalars(select(CatalogDocumentRevision)).all()) == 1


@pytest.mark.usefixtures("damaged_json_rows")
def test_verified_import_supersedes_a_damaged_historical_digest():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = CatalogService(
        sessions, clock=lambda: datetime.now(UTC), cursors=CursorCodec(b"c" * 32)
    )
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    service.import_catalog_models("test", [document])
    with engine.begin() as connection:
        connection.execute(
            update(CatalogDocumentRevision).values(content_digest="f" * 64)
        )
    service.import_catalog_models("test", [document])
    service.import_catalog_models("test", [document])
    with sessions() as session:
        rows = session.scalars(
            select(CatalogDocumentRevision).order_by(
                CatalogDocumentRevision.revision_number
            )
        ).all()
        assert len(rows) == 2
        assert (
            read_catalog_document(rows[-1]).identity.slug
            == document["identity"]["slug"]
        )


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("changed_content", [False, True])
def test_verified_import_supersedes_an_unreadable_pending_candidate(changed_content):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    candidate = CatalogEntityService(
        sessions, clock=lambda: datetime.now(UTC)
    ).create_draft(document, actor="test")
    with engine.begin() as connection:
        connection.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == candidate.id)
            .values(document={"broken": True}, projected={"broken": True})
        )
    service = CatalogService(
        sessions, clock=lambda: datetime.now(UTC), cursors=CursorCodec(b"c" * 32)
    )
    if changed_content:
        document["metadata"]["description"] = "fresh verified content"
    service.import_catalog_models("test", [document])
    service.import_catalog_models("test", [document])
    with sessions() as session:
        rows = session.scalars(
            select(CatalogDocumentRevision).order_by(
                CatalogDocumentRevision.revision_number
            )
        ).all()
        assert len(rows) == (2 if changed_content else 1)
        assert (rows[-1].id == candidate.id) is not changed_content
        assert (
            read_catalog_document(rows[-1]).identity.slug
            == document["identity"]["slug"]
        )


@pytest.mark.parametrize("clears", [False, True])
def test_public_library_read_reobserves_and_ends_with_no_retained_gate(clears):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    CatalogService(
        sessions, clock=lambda: datetime.now(UTC), cursors=CursorCodec(b"c" * 32)
    ).import_catalog_models("test", [document])
    calls = 0

    def observe():
        nonlocal calls
        calls += 1
        if calls <= (2 if clears else 3):
            raise OSError("local observation unavailable")
        return {}

    projection = LibraryProjection(
        sessions, local_state=observe, cursors=CursorCodec(b"c" * 32)
    )
    read = projection.models()
    assert calls == 3
    assert len(read.models) == 1
    assert (
        read.models[0].local.controller == AssetAvailability.UNKNOWN.value
    ) is not clears
    # A completed unknown observation owns no gate against the same fresh read.
    fresh = projection.models()
    assert calls == 4
    assert len(fresh.models) == 1
    assert fresh.models[0].identity == read.models[0].identity
    assert fresh.models[0].local.controller != AssetAvailability.UNKNOWN.value


@pytest.mark.parametrize("clears", [False, True])
def test_activity_keeps_healthy_siblings_and_recovers_provider_observations(clears):
    healthy = OperationItem(
        id="healthy",
        kind="test",
        state=LifecycleState.QUEUED.value,
        attempt=0,
        created_at="2026-10-08T00:00:00+00:00",
    )
    damaged = healthy.model_copy(update={"id": "damaged", "created_at": "unreadable"})
    calls = 0

    def observe(_query):
        nonlocal calls
        calls += 1
        row = (
            damaged
            if calls <= (2 if clears else 3)
            else damaged.model_copy(update={"created_at": healthy.created_at})
        )
        return OperationListPage([row, healthy], None, 2)

    provider = OperationProvider(
        family="test",
        list_operations=observe,
        get_operation=lambda _id: healthy,
    )

    def read():
        return merge_operation_providers(
            (provider,),
            cursor=None,
            limit=10,
            state=None,
            node_id=None,
            cursors=CursorCodec(b"c" * 32),
        )

    page = read()
    assert calls == 3
    assert healthy.id in [OperationItem.model_validate(row).id for row in page.items]
    assert len(page.items) == (2 if clears else 1)
    assert page.total == 2
    assert page.continuation_unavailable is not clears
    restored = read()
    assert calls == 4
    assert len(restored.items) == 2 and restored.projection_issue is None


@pytest.mark.usefixtures("damaged_json_rows")
def test_exact_ingress_restores_missing_root_and_selected_consumer():
    from sqlalchemy import delete
    from vonk_control.catalog_queries import active_head_revision
    from vonk_control.models import CatalogDocument, CatalogDocumentHead

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = CatalogService(
        sessions, clock=lambda: datetime.now(UTC), cursors=CursorCodec(b"c" * 32)
    )
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    service.import_catalog_models("test", [document])
    with sessions() as session:
        row = session.scalar(select(CatalogDocumentRevision))
        assert row is not None
        exact_id, root_id = row.id, row.document_id
    with engine.begin() as connection:
        connection.execute(delete(CatalogDocumentHead))
        connection.execute(delete(CatalogDocument))
    service.import_catalog_models("test", [document])
    with sessions() as session:
        selected = session.scalar(
            select(CatalogDocumentRevision).where(active_head_revision())
        )
        assert selected is not None and selected.id == exact_id
        assert session.get(CatalogDocument, root_id) is not None
    projection = LibraryProjection(sessions, cursors=CursorCodec(b"c" * 32))
    assert [item.identity.content_sha256 for item in projection.models().models] == [
        selected.content_digest
    ]
    service.import_catalog_models("test", [document])
    assert len(projection.models().models) == 1


@pytest.mark.parametrize("clears", [False, True])
def test_activity_detail_observes_provider_faults_with_a_finite_budget(clears):
    from vonk_control.operation_api.providers import get_operation_from_providers

    calls = 0
    healthy = OperationItem(
        id="same",
        kind="test",
        state=LifecycleState.QUEUED.value,
        attempt=0,
        created_at="2026-10-08T00:00:00+00:00",
        supported_actions=["cancel"],
    )

    def fetch(_id):
        nonlocal calls
        calls += 1
        if calls <= (2 if clears else 3):
            raise OSError("unreadable local provider")
        return healthy

    faulty = OperationProvider(
        family="test",
        list_operations=lambda _q: OperationListPage([], None, 0),
        get_operation=fetch,
    )
    readable = OperationProvider(
        family="other",
        list_operations=lambda _q: OperationListPage([], None, 0),
        get_operation=lambda _id: healthy,
    )
    observed = get_operation_from_providers((faulty, readable), "same")
    assert calls == 3
    assert observed.id == healthy.id and observed.state == healthy.state
    assert observed.supported_actions == (["cancel"] if clears else [])
    # Ending an unavailable read cannot block the same identity's fresh read.
    assert get_operation_from_providers((faulty, readable), "same") == healthy
    assert calls == 4
