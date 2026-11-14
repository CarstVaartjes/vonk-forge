"""A published display reaches the library text through every sync, on PostgreSQL.

Every case reads a signed recipe release through the production reader and
applies it through the production sync, so the display pass is exercised where
it runs: after each import and for every recipe the library lists.
"""

from __future__ import annotations

import copy
import itertools
import json
import re
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Self

import pytest
from sqlalchemy import Engine, event, func, inspect, select, text
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import CatalogSyncState
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.catalog_sync import _controller_marker as controller_marker
from vonk_control.catalog_sync_contract import (
    CatalogSyncTrigger,
    ManagedCatalogSyncRequest,
)
from vonk_control.db.schema import reconcile_schema
from vonk_control.library_projection import LibraryProjection
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
)
from vonk_control.recipe_packages import RecipePackageClient
from vonk_control.source_bundles import SourceBundleStore
from vonk_forge_contracts import (
    ModelDisplay,
    RecipeDisplay,
)

from .signed_recipe_release import SignedRecipeRelease, signed_recipe_releases
from .test_recipe_package_transport import _canonical_package_fixture

pytestmark = pytest.mark.usefixtures(signed_recipe_releases.__name__)


class World:
    """One Controller catalog on PostgreSQL, fed by a signed release it can republish."""

    def __init__(self, engine: Engine, root: Path) -> None:
        Base.metadata.create_all(engine)
        self.engine = engine
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        ticks = itertools.count()
        base = datetime(2026, 10, 29, tzinfo=UTC)
        # Every read of the clock moves it, so any write shows in updated_at.
        self.clock = lambda: base + timedelta(seconds=next(ticks))
        cursors = TokenCodec(b"d" * 32).cursor_codec()
        self.catalog = CatalogService(
            self.sessions,
            clock=self.clock,
            cursors=cursors,
            source_bundles=SourceBundleStore(root / "bundles"),
        )
        self.projection = LibraryProjection(
            self.sessions, cursors=cursors, clock=self.clock
        )
        index, row, package = _canonical_package_fixture()
        self.index: dict = json.loads(index)
        self.packages = {row["package"]["path"]: package}  # type: ignore[index]
        self.release = SignedRecipeRelease(self.index, self.packages)
        self.cache = root / "packages"
        self.reader = self.release.client(self.cache)
        self.sync_service = self._sync_service(self.reader)

    def _sync_service(
        self, reader: RecipePackageClient
    ) -> ManagedRecipeCatalogSyncService:
        return ManagedRecipeCatalogSyncService(
            self.sessions, catalog=self.catalog, reader=reader, clock=self.clock
        )

    def publish(self, *, recipe: dict | None = None, model: dict | None = None) -> None:
        """Republish the same documents and packages with other display."""
        index = copy.deepcopy(self.index)
        if recipe is not None:
            index["recipes"][0]["display"].update(recipe)  # type: ignore[index]
        if model is not None:
            index["catalog_entities"][0]["display"].update(model)  # type: ignore[index]
        self.release.publish(index, self.packages)

    def sync(self, reader: RecipePackageClient | None = None):
        service = self.sync_service if reader is None else self._sync_service(reader)
        return service.sync(
            ManagedCatalogSyncRequest(
                request_key=str(__import__("uuid").uuid4()),
                trigger=CatalogSyncTrigger.MANUAL,
                actor="test",
            )
        )

    def revisions(self) -> list[tuple[str, int, str]]:
        with self.sessions() as session:
            return sorted(
                session.execute(
                    select(
                        CatalogDocumentRevision.id,
                        CatalogDocumentRevision.revision_number,
                        CatalogDocumentRevision.content_digest,
                    )
                ).all()
            )

    def documents(self) -> dict[str, tuple[str, datetime, dict | None]]:
        with self.sessions() as session:
            return {
                row.kind: (row.title, row.updated_at, row.display)
                for row in session.scalars(select(CatalogDocument))
            }

    def library_recipe(self):
        self.projection.refresh_catalog(observe_storage=False)
        (recipe,) = self.projection.recipe_library(limit=10).recipes
        return recipe

    def library_model(self):
        self.projection.refresh_catalog(observe_storage=False)
        (model,) = self.projection.models(limit=10).models
        return model


@pytest.fixture
def world(postgres_engine: Engine, tmp_path: Path) -> World:
    built = World(postgres_engine, tmp_path)
    built.release.publish(built.index, built.packages)
    return built


def test_a_display_only_release_changes_the_library_text_and_creates_no_revision(
    world: World,
) -> None:
    first = world.sync()
    assert first.state == CatalogSyncState.CURRENT and first.imported_count == 1
    before = world.revisions()
    assert len(before) == 2
    recipe = world.library_recipe()
    assert recipe.identity.title == "Tiny Recipe"
    assert recipe.recipe_display is not None

    # Same documents, same digests, same packages: only the display moved.
    world.publish(
        recipe={
            "title": "Tiny Recipe, retitled",
            "description": "Reworded for people.",
            "tags": ["fixture", "reworded"],
        }
    )
    second = world.sync()

    assert second.state == CatalogSyncState.CURRENT
    assert second.unchanged_count == 1 and second.imported_count == 0
    recipe = world.library_recipe()
    assert recipe.identity.title == "Tiny Recipe, retitled"
    assert recipe.identity.description == "Reworded for people."
    assert recipe.recipe_display is not None
    assert recipe.recipe_display.tags == ["fixture", "reworded"]
    assert world.documents()["recipe"][0] == "Tiny Recipe, retitled"
    assert world.revisions() == before


def test_a_release_with_unchanged_display_writes_nothing(world: World) -> None:
    world.sync()
    world.publish(recipe={"description": "Moved once."})
    world.sync()
    settled = world.documents()

    again = world.sync()

    assert again.state == CatalogSyncState.CURRENT and again.unchanged_count == 1
    assert world.documents() == settled


def test_model_display_updates_the_model_title_the_same_way(world: World) -> None:
    world.sync()
    before = world.revisions()
    assert world.library_model().model_display is not None

    world.publish(model={"model_title": "Tiny Model, retitled"})
    world.sync()

    model = world.library_model()
    assert model.model_display is not None
    assert model.model_display.model_title == "Tiny Model, retitled"
    assert world.documents()["model"][0] == "Tiny Model, retitled"
    assert world.revisions() == before


def test_a_bad_display_is_that_entrys_problem_and_the_rest_still_apply(
    world: World,
) -> None:
    world.sync()
    index = copy.deepcopy(world.index)
    index["catalog_entities"][0]["display"]["identity"]["slug"] = "another-model"
    index["recipes"][0]["display"]["title"] = "Applied despite its neighbour"
    world.release.publish(index, world.packages)

    snapshot = world.reader.list()

    # A display note: the Model still applies, nothing is skipped, and the
    # snapshot stays complete (so retraction is not paused).
    assert snapshot.problems == ()
    assert len(snapshot.display_notes) == 1
    assert snapshot.display_notes[0].detail.startswith(
        "index catalog_entities display dropped"
    )
    assert len(snapshot.catalog_entities) == 1
    assert snapshot.catalog_entities[0].entry.display is None
    result = world.sync()
    assert result.skipped_count == 0
    assert len(result.problems) == 1
    assert world.library_recipe().identity.title == "Applied despite its neighbour"
    assert world.library_model().model_display is not None


@pytest.mark.parametrize("breakage", ["foreign", "capability"])
def test_an_unreadable_model_display_costs_the_display_only_and_is_read_again(
    world: World, breakage: str
) -> None:
    index = copy.deepcopy(world.index)
    shown = copy.deepcopy(index["catalog_entities"][0]["display"])
    broken = index["catalog_entities"][0]["display"]
    if breakage == "foreign":
        broken["identity"]["slug"] = "another-model"
    else:
        # A capability name this contract does not know: kept by the reader,
        # withheld by the Controller.
        broken["capabilities"] = ["Not_A_Name"]
    world.release.publish(index, world.packages)

    first = world.reader.list()
    result = world.sync()

    assert first.problems == ()
    [note] = first.display_notes
    assert note.detail.startswith("index catalog_entities display")
    assert "fixture/tiny-model" in note.detail
    assert first.catalog_entities[0].entry.display is None
    assert result.skipped_count == 0
    assert world.library_model().model_display is None
    assert world.documents()["model"]

    index["catalog_entities"][0]["display"] = shown
    world.release.publish(index, world.packages)
    second = world.reader.list()
    world.sync()

    assert second.display_notes == ()
    assert world.library_model().model_display is not None


def test_a_display_that_becomes_unreadable_leaves_the_shown_one_in_place(
    world: World,
) -> None:
    world.sync()
    index = copy.deepcopy(world.index)
    index["catalog_entities"][0]["display"]["capabilities"] = ["Not_A_Name"]
    world.release.publish(index, world.packages)

    result = world.sync()

    assert result.skipped_count == 0
    assert world.library_model().model_display is not None


@pytest.mark.parametrize("breakage", ["missing", "invalid", "foreign"])
def test_a_recipe_without_a_usable_display_keeps_its_text_and_stays_offered(
    world: World, breakage: str
) -> None:
    world.sync()
    before = world.revisions()
    index = copy.deepcopy(world.index)
    entry = index["recipes"][0]
    if breakage == "missing":
        del entry["display"]
    elif breakage == "invalid":
        entry["display"]["title"] = ""
    else:
        entry["display"]["identity"]["slug"] = "someone-else"
    world.release.publish(index, world.packages)

    snapshot = world.reader.list()
    result = world.sync()

    # The block is dropped and noted; the recipe stays, titled as it was.
    assert snapshot.problems == ()
    [note] = snapshot.display_notes
    assert note.detail.startswith("index recipes display dropped")
    assert [item.display for item in snapshot.items] == [None]
    assert result.withdrawn_count == 0
    assert world.revisions() == before
    assert world.library_recipe().identity.title == "Tiny Recipe"


def test_a_restart_applies_the_display_of_the_persisted_snapshot(
    world: World,
) -> None:
    snapshot = world.reader.list()
    world.reader.prepare(snapshot)
    world.reader.close()
    world.publish(recipe={"title": "Never fetched"})
    world.release.offline = True

    restarted = world.release.client(world.cache)
    result = world.sync(restarted)

    assert result.state == CatalogSyncState.CURRENT
    assert world.library_recipe().identity.title == "Tiny Recipe"
    assert world.library_recipe().recipe_display is not None
    restarted.close()


def test_a_persisted_snapshot_without_display_keeps_the_text_until_the_next_fetch(
    world: World,
) -> None:
    world.sync()
    before = world.revisions()
    texts = world.documents()
    # The Controller held an index published before display existed.
    older = copy.deepcopy(world.index)
    del older["recipes"][0]["display"]
    del older["catalog_entities"][0]["display"]
    world.release.publish(older, world.packages)
    world.reader.prepare(world.reader.list())
    world.reader.close()
    world.release.offline = True

    restarted = world.release.client(world.cache)
    snapshot = restarted.list()
    result = world.sync(restarted)

    assert [item.display for item in snapshot.items] == [None]
    assert [model.entry.display for model in snapshot.catalog_entities] == [None]
    assert snapshot.problems == () and len(snapshot.display_notes) == 2
    assert result.withdrawn_count == 0
    assert world.revisions() == before
    assert world.documents() == texts
    heads = world.sessions().scalars(select(CatalogDocumentHead)).all()
    assert len(heads) == 2 and all(head.active_revision_id for head in heads)

    # The contract version is part of what a stored sync receipt must match,
    # so the Controller that can read display fetches again.
    assert (
        f"vonk-forge-public-contracts={metadata.version('vonk-forge-public-contracts')}"
        in controller_marker()
    )
    world.release.offline = False
    world.publish(recipe={"title": "After the fetch"})
    fetched = world.release.client(world.cache)
    world.sync(fetched)
    assert world.library_recipe().identity.title == "After the fetch"
    fetched.close()
    restarted.close()


def test_startup_reconcile_adds_the_display_column_and_keeps_the_rows(
    world: World,
) -> None:
    world.sync()
    rows = world.documents()
    with world.engine.begin() as connection:
        connection.execute(text("ALTER TABLE catalog_documents DROP COLUMN display"))
    assert "display" not in {
        column["name"]
        for column in inspect(world.engine).get_columns("catalog_documents")
    }

    with world.engine.begin() as connection:
        reconcile_schema(connection)

    assert "display" in {
        column["name"]
        for column in inspect(world.engine).get_columns("catalog_documents")
    }
    kept = world.documents()
    assert {kind: row[0] for kind, row in kept.items()} == {
        kind: row[0] for kind, row in rows.items()
    }
    assert all(row[2] is None for row in kept.values())
    with world.sessions() as session:
        recipe = session.scalar(
            select(CatalogDocument).where(CatalogDocument.kind == "recipe")
        )
        assert recipe is not None and recipe.recipe_display() is None
    assert world.library_recipe().recipe_display is None

    world.sync()
    filled = world.documents()
    assert all(row[2] is not None for row in filled.values())
    assert world.library_recipe().recipe_display is not None
    assert (
        world.sessions().scalar(select(func.count()).select_from(CatalogDocument)) == 2
    )


class Statements:
    """The SQL statements an action sends, so cost can be asserted."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.sent: list[str] = []

    def __enter__(self) -> Self:
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._record)

    def _record(self, _c, _cursor, statement, _p, _context, _many) -> None:
        self.sent.append(statement)

    def selecting(self, table: str) -> list[str]:
        return [
            statement
            for statement in self.sent
            if statement.lstrip().upper().startswith("SELECT")
            and re.search(rf"\b{table}\b", statement)
        ]


def _seed(world: World, count: int) -> list[RecipeDisplay | ModelDisplay]:
    displays: list[RecipeDisplay | ModelDisplay] = []
    now = datetime(2026, 10, 29, tzinfo=UTC)
    with world.sessions.begin() as session:
        for number in range(count):
            slug = f"seeded-{number:04d}"
            kind = "model" if number % 2 else "recipe"
            session.add(
                CatalogDocument(
                    kind=kind,
                    publisher="seed",
                    slug=slug,
                    title="Before",
                    created_by="test",
                    created_at=now,
                    updated_at=now,
                )
            )
            identity = {"publisher": "seed", "slug": slug}
            displays.append(
                ModelDisplay.model_validate(
                    {
                        "identity": identity,
                        "family_title": "Family",
                        "model_title": "After",
                        "description": "d",
                        "tags": [],
                        "modalities": ["text"],
                        "capabilities": [],
                        "license": {
                            "spdx": "Apache-2.0",
                            "url": "https://example.invalid/license",
                            "attribution": [],
                        },
                    }
                )
                if kind == "model"
                else RecipeDisplay.model_validate(
                    {
                        "identity": identity,
                        "title": "After",
                        "description": "d",
                        "tags": [],
                    }
                )
            )
    return displays


@pytest.mark.parametrize("count", [10, 600])
def test_the_display_pass_reads_once_however_many_documents_it_covers(
    world: World, count: int
) -> None:
    displays = _seed(world, count)

    with Statements(world.engine) as first:
        assert world.catalog.apply_displays(displays) == []
    with Statements(world.engine) as settled:
        assert world.catalog.apply_displays(displays) == []

    assert len(first.selecting("catalog_documents")) == 1
    assert len(settled.selecting("catalog_documents")) == 1
    assert not [s for s in settled.sent if s.lstrip().upper().startswith("UPDATE")]
    assert {row[0] for row in world.documents().values()} <= {"After", "Tiny Recipe"}


def test_a_library_request_reads_no_catalog_document(world: World) -> None:
    world.sync()
    world.library_recipe()
    with Statements(world.engine) as requests:
        world.projection.recipe_library(limit=10)
        world.projection.models(limit=10)
    assert requests.selecting("catalog_documents") == []


def test_a_failed_display_write_is_that_documents_problem_and_the_next_tick_heals_it(
    world: World,
) -> None:
    world.sync()
    world.publish(recipe={"title": "Retitled on the wire"})

    def refuse(_c, _cursor, statement, parameters, _context, _many) -> None:
        if statement.lstrip().upper().startswith("UPDATE CATALOG_DOCUMENTS"):
            raise RuntimeError("disk full")

    event.listen(world.engine, "before_cursor_execute", refuse)
    try:
        failed = world.sync()
    finally:
        event.remove(world.engine, "before_cursor_execute", refuse)

    assert failed.state == CatalogSyncState.PARTIAL
    assert [problem.detail for problem in failed.problems] == ["disk full"]
    assert failed.problems[0].recipe_uri is not None
    assert world.library_recipe().identity.title == "Tiny Recipe"

    healed = world.sync()

    assert healed.state == CatalogSyncState.CURRENT and healed.problems == ()
    assert world.library_recipe().identity.title == "Retitled on the wire"
