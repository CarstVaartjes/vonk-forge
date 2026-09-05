from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.models import Base, CatalogDocumentRevision
from vonk_control.recipe_library import RecipeLibraryItem, RecipeLibrarySnapshot
from vonk_control.recipe_packages import PACKAGE_MEDIA_TYPE, RecipePackageClient
from vonk_control.source_bundles import SourceBundleStore

ROOT = Path(
    os.environ.get(
        "VONK_RECIPE_CANDIDATE_ROOT",
        "/private/tmp/vonk-forge-recipes-contract-conversion-final",
    )
)


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
    row = index["recipes"][0]
    package = (ROOT / row["package"]["path"]).read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(index).encode())
        return httpx.Response(200, headers={"content-type": PACKAGE_MEDIA_TYPE}, content=package)

    client = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx.MockTransport(handler),
    )
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
        expected_commit=snapshot.commit,
    )

    assert result.state == "current"
    assert result.imported_count == 1
    assert reader.fetches == [item.uri]
    with sessions() as session:
        revisions = session.scalars(select(CatalogDocumentRevision)).all()
        assert len([row for row in revisions if row.kind == "model"]) == 92
        assert len([row for row in revisions if row.kind == "recipe"]) == 1
        assert all(row.state == "active" for row in revisions)
    client.close()
