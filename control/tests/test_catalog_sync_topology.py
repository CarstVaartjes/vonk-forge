"""Kit-valid topology changes advance catalog heads without changing workloads."""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import files

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.catalog_service import RecipeCatalogLocalRevision
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.catalog_sync_contract import (
    CatalogSyncTrigger,
    ManagedCatalogSyncRequest,
)
from vonk_control.models import Base
from vonk_control.recipe_library_types import RecipeLibraryItem, RecipeLibrarySnapshot
from vonk_forge_contracts import document_sha256

_EXAMPLES = files("vonk_forge_contracts").joinpath("examples")


def _example(name: str) -> dict[str, object]:
    return json.loads(_EXAMPLES.joinpath(name).read_text(encoding="utf-8"))


def _item(document: dict[str, object]) -> RecipeLibraryItem:
    identity = document["identity"]
    assert isinstance(identity, dict)
    digest = document_sha256(document)
    return RecipeLibraryItem(
        library_commit="c" * 40,
        source_path="recipes/x.json",
        publisher=identity["publisher"],
        slug=identity["slug"],
        title="x",
        description="x",
        tags=(),
        content_sha256=digest,
        uri=f"vonk://catalog/{identity['publisher']}/{identity['slug']}@sha256:{digest}",
        document=document,
    )


class _Reader:
    def __init__(self, item: RecipeLibraryItem) -> None:
        self.item = item

    def list(self) -> RecipeLibrarySnapshot:
        return RecipeLibrarySnapshot("c" * 40, (self.item,))

    def fetch(self, uri: str) -> RecipeLibraryItem:
        return self.item


class _Catalog:
    def __init__(self, node_count: int | None) -> None:
        self.node_count = node_count
        self.imported: list[str] = []

    def refresh_build_policy(self):
        return ()

    def import_catalog_models(self, actor, documents) -> int:
        return 0

    def retract_recipes_absent_from(self, published):
        return []

    def store_source_bundle(self, *args) -> None:
        pass

    def recipe_catalog_local_revisions(self, identities):
        return {
            identity: RecipeCatalogLocalRevision(
                recipe_id="r",
                source_kind="recipe_library",
                publisher=identity[0],
                slug=identity[1],
                revision_number=1,
                content_sha256="0" * 64,
                release_version="1.0.0",
                node_count=self.node_count,
            )
            for identity in identities
            if self.node_count is not None
        }

    def import_recipe_library(self, actor, **kwargs) -> None:
        self.imported.append(kwargs["expected_content_sha256"])


def _sync(tmp_path, catalog: _Catalog, item: RecipeLibraryItem):
    engine = create_engine(f"sqlite:///{tmp_path / 'sync.sqlite'}")
    Base.metadata.create_all(engine)
    service = ManagedRecipeCatalogSyncService(
        sessionmaker(engine, expire_on_commit=False),
        catalog=catalog,  # type: ignore[arg-type]
        reader=_Reader(item),
        clock=lambda: datetime(2026, 9, 29, tzinfo=UTC),
    )
    return service.sync(
        ManagedCatalogSyncRequest(
            request_key=str(uuid.uuid4()),
            trigger=CatalogSyncTrigger.MANUAL,
            actor="test",
        )
    )


def test_sync_accepts_new_topology_without_renaming_content(tmp_path) -> None:
    dual = deepcopy(_example("recipe-dual.json"))
    single = _example("recipe-source-build.json")
    # The same recipe id, but now needing two Sparks instead of one.
    dual["identity"] = single["identity"]
    item = _item(dual)
    catalog = _Catalog(node_count=1)

    result = _sync(tmp_path, catalog, item)

    assert catalog.imported == [item.content_sha256]
    assert result.state == "current"
    assert result.skipped_count == 0
    assert result.problems == ()
    # A fresh authorized publication remains eligible after this acceptance.
    _sync(tmp_path, catalog, item)
    assert catalog.imported == [item.content_sha256, item.content_sha256]


def test_sync_imports_a_revision_with_the_same_spark_count(tmp_path) -> None:
    single = _example("recipe-source-build.json")
    item = _item(single)
    catalog = _Catalog(node_count=1)

    result = _sync(tmp_path, catalog, item)

    assert catalog.imported == [item.content_sha256]
    assert result.state == "current"
    assert result.problems == ()


def test_stale_local_identity_is_a_miss_and_cannot_veto_verified_import(tmp_path):
    class StaleCatalog(_Catalog):
        def recipe_catalog_local_revisions(self, identities):
            return {
                key: replace(value, publisher="stale-bookkeeping")
                for key, value in super()
                .recipe_catalog_local_revisions(identities)
                .items()
            }

    item = _item(_example("recipe-source-build.json"))
    catalog = StaleCatalog(node_count=1)
    _sync(tmp_path, catalog, item)
    assert catalog.imported == [item.content_sha256]
    _sync(tmp_path, catalog, item)
    assert catalog.imported == [item.content_sha256, item.content_sha256]
