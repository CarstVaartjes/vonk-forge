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


def test_local_library_observation_recovers_on_the_next_read():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    observation = [False]

    def observe():
        if not observation[0]:
            raise OSError("local observation unavailable")
        return {}

    projection = LibraryProjection(
        sessionmaker(engine),
        local_state=observe,
        cursors=CursorCodec(b"c" * 32),
    )
    snapshot = projection._local_state_snapshot()
    assert (
        projection._local("a" * 64, kind="model", snapshot=snapshot).controller
        == AssetAvailability.UNKNOWN.value
    )
    observation[0] = True
    assert (
        projection._local(
            "a" * 64, kind="model", snapshot=projection._local_state_snapshot()
        ).controller
        != AssetAvailability.UNKNOWN.value
    )


def test_activity_keeps_healthy_siblings_and_recovers_provider_observations():
    healthy = OperationItem(
        id="healthy",
        kind="test",
        state=LifecycleState.QUEUED.value,
        attempt=0,
        created_at="2026-10-08T00:00:00+00:00",
    )
    damaged = healthy.model_copy(update={"id": "damaged", "created_at": "unreadable"})
    rows = [damaged, healthy]
    provider = OperationProvider(
        family="test",
        list_operations=lambda _query: OperationListPage(rows, None, 2),
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
    assert [OperationItem.model_validate(row).id for row in page.items] == [healthy.id]
    assert page.total == 2 and page.projection_issue is not None
    rows[0] = damaged.model_copy(update={"created_at": healthy.created_at})
    restored = read()
    assert len(restored.items) == 2 and restored.projection_issue is None
