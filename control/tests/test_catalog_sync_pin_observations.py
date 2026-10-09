"""A partial sync cannot interpret a missing image pin as withdrawal."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import CatalogSyncCode
from vonk_control.auth import CursorCodec
from vonk_control.catalog_revision_contract import (
    PrebuiltImage,
    RecipeRevisionProjection,
    read_catalog_projection,
)
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.catalog_sync_contract import (
    CatalogSyncTrigger,
    ManagedCatalogSyncProblem,
    ManagedCatalogSyncRequest,
)
from vonk_control.models import Base, CatalogDocumentRevision, RecipeLibrarySyncRun
from vonk_control.recipe_library_types import RecipeLibrarySnapshot

from .test_catalog_entities import _model, _recipe
from .test_catalog_sync_topology import _item


@pytest.mark.parametrize("partial", [False, True])
def test_sync_withdraws_pins_only_after_a_complete_authoritative_observation(partial):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions, clock=lambda: datetime.now(UTC), cursors=CursorCodec(b"c" * 32)
    )
    image = PrebuiltImage(
        reference="ghcr.io/vonk/image@sha256:" + "a" * 64, build_key="b" * 64
    )
    item = replace(
        _item(_recipe(_model())), dependencies=(_model(),), prebuilt_image=image
    )

    class Reader:
        snapshot = RecipeLibrarySnapshot("c" * 40, (item,))

        def list(self):
            return self.snapshot

        def fetch(self, uri):
            return self.snapshot.items[0]

    reader = Reader()
    sync = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=reader, clock=lambda: datetime.now(UTC)
    )

    def apply(trigger):
        result = sync.sync(
            ManagedCatalogSyncRequest(
                request_key=str(uuid.uuid4()), trigger=trigger, actor="test"
            )
        )
        assert result.completed_at is not None
        with sessions() as session:
            receipt = session.get(RecipeLibrarySyncRun, result.id)
            assert receipt is not None and receipt.active_slot is None
            row = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == item.document["kind"]
                )
            )
            assert row is not None
            projection = read_catalog_projection(row)
            assert isinstance(projection, RecipeRevisionProjection)
            return row.id, projection.prebuilt_image

    revision, pinned = apply("manual")
    assert pinned == image
    problem = ManagedCatalogSyncProblem(
        recipe_uri=None,
        code=CatalogSyncCode.ITEM_FAILED,
        detail="incomplete observation",
    )
    reader.snapshot = replace(
        reader.snapshot,
        items=(replace(item, prebuilt_image=None),),
        problems=(problem,) if partial else (),
    )
    # A manual view cannot withdraw even an explicitly omitted pin.
    assert apply("manual") == (revision, image)
    assert apply("automatic") == (revision, image if partial else None)
    reader.snapshot = replace(reader.snapshot, problems=())
    assert apply("automatic") == (revision, None)
    reader.snapshot = replace(reader.snapshot, items=(item,))
    assert apply("manual") == (revision, image)


def test_policy_miss_is_reobserved_without_reusing_a_false_current_receipt(monkeypatch):
    import vonk_control.catalog_entities as entities

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions, clock=lambda: datetime.now(UTC), cursors=CursorCodec(b"c" * 32)
    )
    item = replace(_item(_recipe(_model())), dependencies=(_model(),))

    class Reader:
        def list(self):
            return RecipeLibrarySnapshot("c" * 40, (item,))

        def fetch(self, uri):
            return item

    sync = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=Reader(), clock=lambda: datetime.now(UTC)
    )
    first = sync.automatic()
    local = catalog.recipe_catalog_local_revisions([(item.publisher, item.slug)])

    def unavailable_policy(_recipe):
        raise OSError("compiler dependency unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(entities, "build_policy_projection", unavailable_policy)
        ended = sync.automatic()
        assert ended.id != first.id and ended.completed_at is not None
        assert (
            catalog.recipe_catalog_local_revisions([(item.publisher, item.slug)])
            == local
        )
        with sessions() as session:
            row = session.get(RecipeLibrarySyncRun, ended.id)
            assert row is not None and row.active_slot is None
    restored = sync.automatic()
    assert restored.id != ended.id and restored.completed_at is not None
    assert (
        catalog.recipe_catalog_local_revisions([(item.publisher, item.slug)]) == local
    )
    fresh = sync.sync(
        ManagedCatalogSyncRequest(
            request_key=str(uuid.uuid4()),
            trigger=CatalogSyncTrigger.MANUAL,
            actor="test",
        )
    )
    assert fresh.completed_at is not None and fresh.unchanged_count == 1
