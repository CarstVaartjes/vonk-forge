from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService, RecipeDraftInput
from vonk_control.catalog_sync import CatalogSyncError, ManagedRecipeCatalogSyncService
from vonk_control.models import (
    Base,
    ManagedRecipeLibraryLink,
    RecipeInstallation,
    RecipeLibrarySyncRun,
)
from vonk_control.recipe_contract import recipe_content_sha256
from vonk_control.recipe_library import RecipeLibraryItem, RecipeLibrarySnapshot
from vonk_control.source_bundles import SourceBundleStore

from .test_catalog_service import _seed_recipe_dependencies

NOW = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
REPOSITORY = "CarstVaartjes/vonk-forge-recipes"


class Reader:
    def __init__(self, snapshot: RecipeLibrarySnapshot) -> None:
        self.snapshot = snapshot
        self.fetches: list[str] = []

    def list(self) -> RecipeLibrarySnapshot:
        return self.snapshot

    def fetch(self, uri: str) -> RecipeLibraryItem:
        self.fetches.append(uri)
        return next(item for item in self.snapshot.items if item.uri == uri)


@pytest.fixture
def catalog(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog-sync.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = CatalogService(
        sessions,
        clock=lambda: NOW,
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "bundles"),
    )
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    document["metadata"]["tags"].append("executable")
    _seed_recipe_dependencies(service, document)
    return sessions, service, document


def _item(document: dict[str, object], commit: str) -> RecipeLibraryItem:
    digest = recipe_content_sha256(document)
    identity = document["identity"]
    metadata = document["metadata"]
    return RecipeLibraryItem(
        library_commit=commit,
        source_path=f"recipes/{identity['slug']}.json",
        publisher=identity["publisher"],
        slug=identity["slug"],
        title=metadata["title"],
        description=metadata["description"],
        tags=tuple(metadata["tags"]),
        content_sha256=digest,
        uri=(
            f"vonk://catalog/{identity['publisher']}/{identity['slug']}"
            f"@sha256:{digest}"
        ),
        document=document,
    )


def _service(catalog, snapshot: RecipeLibrarySnapshot):
    sessions, service, _document = catalog
    reader = Reader(snapshot)
    return (
        ManagedRecipeCatalogSyncService(
            sessions,
            catalog=service,
            reader=reader,
            clock=lambda: NOW,
        ),
        reader,
    )


def test_sync_imports_exact_snapshot_and_replay_is_idempotent(catalog) -> None:
    _sessions, _catalog, document = catalog
    item = _item(document, "a" * 40)
    sync, reader = _service(
        catalog,
        RecipeLibrarySnapshot(commit="a" * 40, items=(item,)),
    )
    request_key = str(uuid.uuid4())

    first = sync.sync(request_key=request_key, trigger="manual", actor="admin")
    replay = sync.sync(request_key=request_key, trigger="manual", actor="admin")

    assert first.state == "current"
    assert first.commit == "a" * 40
    assert first.imported_count == 1
    assert first.updated_count == 0
    assert replay.id == first.id
    assert reader.fetches == [item.uri]
    with sync._sessions() as session:
        link = session.scalar(select(ManagedRecipeLibraryLink))
        run = session.scalar(select(RecipeLibrarySyncRun))
        assert link is not None and link.remote_content_sha256 == item.content_sha256
        assert link.availability == "present"
        assert run is not None and run.state == "succeeded"
        assert run.processed_count == 1


def test_sync_updates_immutable_revision_and_reports_stale_install(catalog) -> None:
    sessions, service, document = catalog
    first_item = _item(document, "a" * 40)
    first, _reader = _service(
        catalog, RecipeLibrarySnapshot(commit="a" * 40, items=(first_item,))
    )
    first.sync(
        request_key=str(uuid.uuid4()), trigger="manual", actor="admin"
    )
    recipe_id = service.recipe_catalog_local_revisions([first_item.slug])[first_item.slug].recipe_id
    initial_revision = service.get_recipe(recipe_id)
    with sessions.begin() as session:
        session.add(
            RecipeInstallation(
                recipe_revision_id=initial_revision.id,
                mapping_id=str(uuid.uuid4()),
                mapping_generation=1,
                recipe_build_id=str(uuid.uuid4()),
                image_digest="sha256:" + "1" * 64,
                plan_digest="2" * 64,
                plan={},
                state="installed",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    changed = copy.deepcopy(document)
    changed["metadata"]["description"] = "A newer managed revision."
    second_item = _item(changed, "b" * 40)
    second, _reader = _service(
        catalog, RecipeLibrarySnapshot(commit="b" * 40, items=(second_item,))
    )

    result = second.sync(
        request_key=str(uuid.uuid4()), trigger="manual", actor="admin"
    )

    assert result.updated_count == 1
    assert result.stale_recipes == (
        {
            "recipe_id": recipe_id,
            "current_revision_id": service.get_recipe(recipe_id).id,
            "stale_installation_count": 1,
            "stale_run_count": 0,
        },
    )
    assert service.get_recipe(recipe_id).revision_number == 2


def test_sync_preserves_custom_recipe_on_slug_collision(catalog) -> None:
    _sessions, service, document = catalog
    service.create_recipe(
        "admin",
        RecipeDraftInput(slug=document["identity"]["slug"], document=document),
    )
    item = _item(document, "a" * 40)
    sync, reader = _service(
        catalog, RecipeLibrarySnapshot(commit="a" * 40, items=(item,))
    )

    result = sync.sync(
        request_key=str(uuid.uuid4()), trigger="manual", actor="admin"
    )

    assert result.state == "partial"
    assert result.skipped_count == 1
    assert result.problems[0]["code"] == "catalog.sync_custom_conflict"
    assert reader.fetches == []
    assert service.get_recipe(
        service.recipe_catalog_local_revisions([item.slug])[item.slug].recipe_id
    ).source_kind == "local"


def test_missing_recipe_is_only_withdrawn_when_installed(catalog) -> None:
    sessions, service, document = catalog
    item = _item(document, "a" * 40)
    sync, _reader = _service(
        catalog, RecipeLibrarySnapshot(commit="a" * 40, items=(item,))
    )
    sync.sync(request_key=str(uuid.uuid4()), trigger="manual", actor="admin")
    recipe_id = service.recipe_catalog_local_revisions([item.slug])[item.slug].recipe_id

    empty, _reader = _service(
        catalog, RecipeLibrarySnapshot(commit="b" * 40, items=())
    )
    irrelevant = empty.sync(
        request_key=str(uuid.uuid4()), trigger="manual", actor="admin"
    )
    assert irrelevant.withdrawn_count == 0
    assert irrelevant.withdrawn_recipes == ()

    revision = service.get_recipe(recipe_id)
    with sessions.begin() as session:
        session.add(
            RecipeInstallation(
                recipe_revision_id=revision.id,
                mapping_id=str(uuid.uuid4()),
                mapping_generation=1,
                recipe_build_id=str(uuid.uuid4()),
                image_digest="sha256:" + "3" * 64,
                plan_digest="4" * 64,
                plan={},
                state="installed",
                actor="admin",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    still_empty, _reader = _service(
        catalog, RecipeLibrarySnapshot(commit="c" * 40, items=())
    )
    relevant = still_empty.sync(
        request_key=str(uuid.uuid4()), trigger="manual", actor="admin"
    )
    assert relevant.withdrawn_count == 1
    assert relevant.withdrawn_recipes[0]["recipe_id"] == recipe_id


def test_sync_expected_commit_and_request_semantics_are_bound(catalog) -> None:
    _sessions, _service_value, document = catalog
    item = _item(document, "a" * 40)
    sync, _reader = _service(
        catalog, RecipeLibrarySnapshot(commit="a" * 40, items=(item,))
    )
    request_key = str(uuid.uuid4())

    with pytest.raises(CatalogSyncError, match="changed since"):
        sync.sync(
            request_key=request_key,
            trigger="manual",
            actor="admin",
            expected_commit="b" * 40,
        )
    failed = sync.latest()
    assert failed is not None and failed.state == "failed"

    with pytest.raises(CatalogSyncError, match="different sync semantics"):
        sync.sync(
            request_key=request_key,
            trigger="manual",
            actor="admin",
            expected_commit="a" * 40,
        )


def test_automatic_sync_runs_once_per_immutable_commit(catalog) -> None:
    _sessions, _service_value, document = catalog
    item = _item(document, "a" * 40)
    sync, reader = _service(
        catalog, RecipeLibrarySnapshot(commit="a" * 40, items=(item,))
    )

    first = sync.automatic()
    repeated = sync.automatic()

    assert first.trigger == "automatic"
    assert repeated.id == first.id
    assert reader.fetches == [item.uri]


def test_failed_managed_update_is_visible_without_overwriting_local_revision(
    catalog,
) -> None:
    sessions, service, document = catalog
    initial_item = _item(document, "a" * 40)
    initial, _reader = _service(
        catalog, RecipeLibrarySnapshot(commit="a" * 40, items=(initial_item,))
    )
    initial.sync(request_key=str(uuid.uuid4()), trigger="manual", actor="admin")
    recipe_id = service.recipe_catalog_local_revisions([initial_item.slug])[
        initial_item.slug
    ].recipe_id
    revision_id = service.get_recipe(recipe_id).id

    invalid = copy.deepcopy(document)
    invalid["metadata"]["description"] = "A revision that lost executable metadata."
    invalid["metadata"]["tags"].remove("executable")
    invalid_item = _item(invalid, "b" * 40)
    update, _reader = _service(
        catalog, RecipeLibrarySnapshot(commit="b" * 40, items=(invalid_item,))
    )

    result = update.sync(
        request_key=str(uuid.uuid4()), trigger="manual", actor="admin"
    )

    assert result.state == "partial"
    assert service.get_recipe(recipe_id).id == revision_id
    with sessions() as session:
        link = session.get(ManagedRecipeLibraryLink, recipe_id)
        assert link is not None
        assert link.sync_state == "update-available"
        assert link.remote_content_sha256 == invalid_item.content_sha256
        assert "executable" in (link.last_error or "")
