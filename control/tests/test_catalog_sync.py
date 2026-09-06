from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import CatalogSyncError, ManagedRecipeCatalogSyncService
from vonk_control.models import Base, CatalogDocumentRevision
from vonk_control.recipe_library_types import (
    RecipeLibraryError,
    RecipeLibraryItem,
    RecipeLibrarySnapshot,
)
from vonk_control.recipe_packages import PACKAGE_MEDIA_TYPE, RecipePackageClient
from vonk_control.source_bundles import SourceBundleStore
from vonk_forge_contracts import RecipeDefinition, content_sha256

ROOT = Path(os.environ.get("VONK_RECIPE_CANDIDATE_ROOT", "/private/tmp/vonk-forge-recipes-contract-conversion-final"))


class Reader:
    def __init__(self, snapshot: RecipeLibrarySnapshot) -> None:
        self.snapshot = snapshot
        self.fetches: list[str] = []

    def list(self) -> RecipeLibrarySnapshot:
        return self.snapshot

    def fetch(self, uri: str) -> RecipeLibraryItem:
        self.fetches.append(uri)
        return next(item for item in self.snapshot.items if item.uri == uri)


def _item_with_document(item: RecipeLibraryItem, document: dict[str, object]) -> RecipeLibraryItem:
    recipe = RecipeDefinition.model_validate(document)
    digest = content_sha256(recipe)
    return replace(
        item,
        content_sha256=digest,
        uri=f"vonk://catalog/{item.publisher}/{item.slug}@sha256:{digest}",
        document=recipe.model_dump(mode="json"),
        tags=tuple(recipe.metadata.tags),
        release_history=(),
        package_handle=None,
        package_sha256=None,
        source_bundle=None,
        source_bundle_sha256=None,
    )


def _fixture(tmp_path: Path) -> tuple[sessionmaker, CatalogService, Reader, RecipeLibraryItem]:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    row = index["recipes"][0]
    package = (ROOT / row["package"]["path"]).read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(index).encode())
        return httpx.Response(200, headers={"content-type": PACKAGE_MEDIA_TYPE}, content=package)

    client = RecipePackageClient("http://127.0.0.1", cache_root=tmp_path / "packages", transport=httpx.MockTransport(handler))
    snapshot = client.list()
    item = client.fetch(snapshot.items[0].uri)
    snapshot = RecipeLibrarySnapshot(snapshot.commit, (item,), snapshot.repository, snapshot.catalog_entities)
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "bundles"),
    )
    return sessions, service, Reader(snapshot), item


class FailOnceReader(Reader):
    def __init__(self, snapshot: RecipeLibrarySnapshot, failing_uri: str) -> None:
        super().__init__(snapshot)
        self._failing_uri = failing_uri
        self._failed = False

    def fetch(self, uri: str) -> RecipeLibraryItem:
        if uri == self._failing_uri and not self._failed:
            self.fetches.append(uri)
            self._failed = True
            raise RecipeLibraryError(
                "recipe_library.unavailable", "transient recipe fetch failure"
            )
        return super().fetch(uri)


def _sync(sessions, service, reader) -> ManagedRecipeCatalogSyncService:
    return ManagedRecipeCatalogSyncService(
        sessions,
        catalog=service,
        reader=reader,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )


def test_sync_imports_canonical_models_and_changed_recipe_once(tmp_path: Path) -> None:
    sessions, service, reader, item = _fixture(tmp_path)
    sync = _sync(sessions, service, reader)
    result = sync.sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )
    assert result.state == "current"
    assert result.imported_count == 1
    assert reader.fetches == [item.uri]
    with sessions() as session:
        revisions = session.scalars(select(CatalogDocumentRevision)).all()
        assert len([row for row in revisions if row.kind == "model"]) == 92
        assert len([row for row in revisions if row.kind == "recipe"]) == 1


def test_sync_keys_local_revisions_by_publisher_and_slug(tmp_path: Path) -> None:
    sessions, service, reader, item = _fixture(tmp_path)

    def variant(*, publisher: str, slug: str) -> RecipeLibraryItem:
        document = deepcopy(item.document)
        identity = document["identity"]
        assert isinstance(identity, dict)
        identity["publisher"] = publisher
        identity["slug"] = slug
        return _item_with_document(
            replace(item, publisher=publisher, slug=slug, source_path=f"recipes/{slug}.json"),
            document,
        )

    first = _sync(sessions, service, reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )
    assert first.state == "current"

    other_publisher = variant(publisher="other-publisher", slug=item.slug)
    same_publisher_a = variant(
        publisher=item.publisher,
        slug=f"{item.slug}-variant-a",
    )
    same_publisher_b = variant(
        publisher=item.publisher,
        slug=f"{item.slug}-variant-b",
    )
    reader.snapshot = replace(
        reader.snapshot,
        items=(item, other_publisher, same_publisher_a, same_publisher_b),
    )
    result = _sync(sessions, service, reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )

    assert result.state == "current"
    assert result.imported_count == 3
    assert result.updated_count == 0
    assert result.skipped_count == 0
    with sessions() as session:
        revisions = session.scalars(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        ).all()
        assert {
            (row.publisher, row.slug)
            for row in revisions
        } == {
            (item.publisher, item.slug),
            (other_publisher.publisher, other_publisher.slug),
            (same_publisher_a.publisher, same_publisher_a.slug),
            (same_publisher_b.publisher, same_publisher_b.slug),
        }
        assert len({row.content_digest for row in revisions}) == 4


def test_local_revision_lookup_accepts_more_than_256_identities(tmp_path: Path) -> None:
    _sessions, service, _reader, _item = _fixture(tmp_path)
    identities = [
        (f"publisher-{index}", f"recipe-{index}") for index in range(257)
    ]

    assert service.recipe_catalog_local_revisions(identities) == {}


def test_sync_imports_canonical_recipe_without_readiness_tags(tmp_path: Path) -> None:
    sessions, service, reader, item = _fixture(tmp_path)
    document = deepcopy(item.document)
    document["metadata"]["tags"] = []  # type: ignore[index]
    replacement = _item_with_document(item, document)
    reader.snapshot = RecipeLibrarySnapshot(
        reader.snapshot.commit,
        (replacement,),
        reader.snapshot.repository,
        reader.snapshot.catalog_entities,
    )

    result = _sync(sessions, service, reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )

    assert result.state == "current"
    assert result.imported_count == 1
    assert result.problems == ()


def test_sync_fails_closed_for_unresolvable_canonical_recipe(tmp_path: Path) -> None:
    sessions, service, reader, item = _fixture(tmp_path)
    document = deepcopy(item.document)
    document["models"][0]["model"]["content_sha256"] = "0" * 64  # type: ignore[index]
    replacement = _item_with_document(item, document)
    reader.snapshot = RecipeLibrarySnapshot(
        reader.snapshot.commit,
        (replacement,),
        reader.snapshot.repository,
        reader.snapshot.catalog_entities,
    )

    result = _sync(sessions, service, reader).sync(
        request_key=str(uuid.uuid4()),
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )

    assert result.state == "partial"
    assert result.skipped_count == 1
    assert result.problems[0]["code"] == "catalog.model_reference_missing"
    with sessions() as session:
        assert session.scalars(select(CatalogDocumentRevision).where(CatalogDocumentRevision.kind == "recipe")).all() == []


def test_recipe_metadata_tags_do_not_change_execution_identity(tmp_path: Path) -> None:
    sessions, service, _reader, item = _fixture(tmp_path)
    service.import_recipe_library(
        "test",
        library_commit=item.library_commit,
        source_path=item.source_path,
        document=item.document,
        expected_content_sha256=item.content_sha256,
        dependency_documents=item.dependencies,
    )
    document = deepcopy(item.document)
    document["metadata"]["tags"] = ["editorial-only"]  # type: ignore[index]
    replacement = _item_with_document(item, document)
    service.import_recipe_library(
        "test",
        library_commit=replacement.library_commit,
        source_path=replacement.source_path,
        document=replacement.document,
        expected_content_sha256=replacement.content_sha256,
        dependency_documents=replacement.dependencies,
    )

    with sessions() as session:
        revisions = session.scalars(
            select(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.kind == "recipe")
            .order_by(CatalogDocumentRevision.revision_number)
        ).all()
        assert len(revisions) == 2
        assert revisions[0].content_digest != revisions[1].content_digest
        assert revisions[0].execution_key == revisions[1].execution_key


def test_automatic_sync_reuses_same_commit_without_refetch(tmp_path: Path) -> None:
    sessions, service, reader, _item_value = _fixture(tmp_path)
    sync = _sync(sessions, service, reader)
    first = sync.automatic()
    repeated = sync.automatic()
    assert repeated.id == first.id
    assert reader.fetches == [reader.snapshot.items[0].uri]


def test_automatic_sync_retries_partial_same_commit_without_refetching_successes(
    tmp_path: Path,
) -> None:
    sessions, service, reader, item = _fixture(tmp_path)
    second_document = deepcopy(item.document)
    second_slug = f"{item.slug}-retry"
    second_document["identity"]["slug"] = second_slug  # type: ignore[index]
    second = _item_with_document(
        replace(item, slug=second_slug, source_path=f"recipes/{second_slug}.json"),
        second_document,
    )
    reader = FailOnceReader(
        replace(
            reader.snapshot,
            items=(reader.snapshot.items[0], second),
        ),
        second.uri,
    )
    sync = _sync(sessions, service, reader)

    partial = sync.automatic()
    assert partial.state == "partial"
    assert partial.skipped_count == 1
    assert reader.fetches == [reader.snapshot.items[0].uri, second.uri]

    recovered = sync.automatic()
    assert recovered.state == "current"
    assert recovered.id != partial.id
    assert recovered.imported_count == 1
    assert recovered.unchanged_count == 1
    assert reader.fetches == [reader.snapshot.items[0].uri, second.uri, second.uri]


def test_sync_rejects_preview_commit_mismatch_without_catalog_mutation(tmp_path: Path) -> None:
    sessions, service, reader, _item_value = _fixture(tmp_path)
    sync = _sync(sessions, service, reader)
    with pytest.raises(CatalogSyncError, match="changed since"):
        sync.sync(
            request_key=str(uuid.uuid4()),
            trigger="manual",
            actor="test",
            expected_commit="b" * 40,
        )
    with sessions() as session:
        assert session.scalars(select(CatalogDocumentRevision)).all() == []
