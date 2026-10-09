from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.models import Base, CatalogDocumentRevision
from vonk_control.recipe_library_types import RecipeLibraryItem, RecipeLibrarySnapshot
from vonk_control.source_bundles import SourceBundleStore

from tests.recipe_library_source import recipe_library_root
from tests.signed_recipe_release import SignedRecipeRelease, signed_recipe_releases

ROOT = recipe_library_root()
pytestmark = pytest.mark.usefixtures(signed_recipe_releases.__name__)


class Reader:
    def __init__(self, snapshot: RecipeLibrarySnapshot) -> None:
        self.snapshot = snapshot
        self.fetches: list[str] = []

    def list(self) -> RecipeLibrarySnapshot:
        return self.snapshot

    def fetch(self, uri: str) -> RecipeLibraryItem:
        self.fetches.append(uri)
        return self.snapshot.items[0]


def test_sync_imports_canonical_models_and_recipe_once(tmp_path: Path) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    index["recipes"] = index["recipes"][:1]
    client = SignedRecipeRelease.from_library(index, ROOT).client(tmp_path / "packages")
    snapshot = client.list()
    item = client.fetch(snapshot.items[0].uri)
    snapshot = RecipeLibrarySnapshot(
        commit=snapshot.commit,
        items=(item,),
        repository=snapshot.repository,
        catalog_entities=snapshot.catalog_entities,
    )
    reader = Reader(snapshot)
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "bundles"),
    )
    sync = ManagedRecipeCatalogSyncService(
        sessions,
        catalog=service,
        reader=reader,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )

    result = sync.sync(
        request_key="00000000-0000-4000-8000-000000000001",
        trigger="manual",
        actor="test",
        reviewed_snapshot=snapshot,
    )

    assert result.state == "current"
    assert result.imported_count == 1
    assert reader.fetches == [item.uri]
    with sessions() as session:
        revisions = session.scalars(select(CatalogDocumentRevision)).all()
        assert len([row for row in revisions if row.kind == "model"]) == len(
            snapshot.catalog_entities
        )
        assert len([row for row in revisions if row.kind == "recipe"]) == 1
        assert all(row.state == "active" for row in revisions)
    client.close()
