"""The signed catalog's API facts follow the sibling partial-snapshot rule.

The facts belong to one exact revision. A complete automatic view withdraws a
revision's facts when the index no longer carries them; a manual or fixture sync
is a deliberately partial view and never withdraws what it does not name.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import files

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import CursorCodec
from vonk_control.catalog_revision_contract import read_recipe_projection
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.catalog_sync_contract import (
    CatalogSyncTrigger,
    ManagedCatalogSyncRequest,
)
from vonk_control.models import Base, CatalogDocumentRevision
from vonk_control.recipe_library_types import RecipeLibrarySnapshot
from vonk_forge_contracts import RecipeApi

from .test_catalog_entities import _model, _recipe
from .test_catalog_sync_topology import _item


def test_facts_are_withdrawn_only_by_a_complete_automatic_view() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    catalog = CatalogService(
        sessions, clock=lambda: now, cursors=CursorCodec(b"c" * 32)
    )
    item = replace(_item(_recipe(_model())), dependencies=(_model(),))
    api = RecipeApi.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-api-decision.json")
            .read_text(encoding="utf-8")
        )
    )

    class Reader:
        snapshot = RecipeLibrarySnapshot("c" * 40, (replace(item, api=api),))

        def list(self):
            return self.snapshot

        def fetch(self, uri):
            return self.snapshot.items[0]

    reader = Reader()
    sync = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=reader, clock=lambda: now
    )

    def run(trigger: CatalogSyncTrigger) -> RecipeApi | None:
        assert (
            sync.sync(
                ManagedCatalogSyncRequest(
                    request_key=str(uuid.uuid4()), trigger=trigger, actor="test"
                )
            ).completed_at
            is not None
        )
        with sessions() as session:
            revision = session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe"
                )
            ).one()
            return read_recipe_projection(revision).api

    assert run(CatalogSyncTrigger.AUTOMATIC) == api

    # The index no longer carries the facts: a manual view is partial and keeps
    # what it does not name; the complete automatic view withdraws them.
    reader.snapshot = replace(reader.snapshot, items=(item,))
    assert run(CatalogSyncTrigger.MANUAL) == api
    assert run(CatalogSyncTrigger.AUTOMATIC) is None
