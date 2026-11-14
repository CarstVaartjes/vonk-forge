"""The signed catalog's qualification status reaches the Library as published.

The status is evidence about one exact revision and changes without the recipe
changing, so every sync records it for unchanged revisions too. It is
informative: an unreadable status (a catalog newer than this Controller's
contract) is absent for that entry and never a sync problem, so nothing waits on
it.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import CatalogSyncState
from vonk_control.auth import CursorCodec, TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.catalog_sync_contract import (
    CatalogSyncTrigger,
    ManagedCatalogSyncRequest,
)
from vonk_control.library_projection import LibraryProjection
from vonk_control.models import Base
from vonk_control.recipe_library_types import RecipeLibrarySnapshot
from vonk_forge_contracts.qualification_status import (
    PublishedQualificationStatusView,
    QualificationStatus,
)

from .signed_recipe_release import signed_recipe_releases
from .test_catalog_display_pass import World
from .test_catalog_entities import _model, _recipe
from .test_catalog_sync_topology import _item


def _status(content_sha256: str, status: str) -> PublishedQualificationStatusView:
    record = {
        "identity": {"content_sha256": content_sha256, "required_sparks": 1},
        "tested_at": "2026-11-01T10:00:00Z",
        "platform": {"controller_version": "1.2.3", "spark_count": 1},
    }
    if status == "Working":
        evidence = {
            "last_pass": record
            | {
                "outcome": "passed",
                "cause_code": "qualification.hardware_pass",
                "cause_summary": "Hardware checks passed",
            }
        }
    else:
        evidence = {
            "last_failure": record
            | {
                "outcome": "failed",
                "blame": "recipe",
                "cause_code": "recipe.runtime_exit",
                "cause_summary": "Recipe runtime exited; needs fix",
            }
        }
    return PublishedQualificationStatusView.model_validate(
        {"status": status, "required_sparks": 1} | evidence
    )


def test_sync_carries_the_published_status_to_the_library_and_follows_new_evidence():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    catalog = CatalogService(
        sessions, clock=lambda: now, cursors=CursorCodec(b"c" * 32)
    )
    item = replace(_item(_recipe(_model())), dependencies=(_model(),))
    working = _status(item.content_sha256, "Working")

    class Reader:
        snapshot = RecipeLibrarySnapshot(
            "c" * 40, (replace(item, qualification=working),)
        )

        def list(self):
            return self.snapshot

        def fetch(self, uri):
            return self.snapshot.items[0]

    reader = Reader()
    sync = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=reader, clock=lambda: now
    )

    def library_status():
        assert (
            sync.sync(
                ManagedCatalogSyncRequest(
                    request_key=str(uuid.uuid4()),
                    trigger=CatalogSyncTrigger.AUTOMATIC,
                    actor="test",
                )
            ).completed_at
            is not None
        )
        projection = LibraryProjection(
            sessions, cursors=TokenCodec(b"p" * 32).cursor_codec(), clock=lambda: now
        )
        (recipe,) = projection.recipe_library(limit=10).recipes
        # One place on the wire: the status is not repeated inside ``projected``.
        assert recipe.projected.qualification is None
        return recipe.qualification

    assert library_status() == working

    # New evidence for the same revision: the unchanged recipe follows it.
    failed = _status(item.content_sha256, "Failed: recipe")
    reader.snapshot = replace(
        reader.snapshot, items=(replace(item, qualification=failed),)
    )
    shown = library_status()
    assert shown is not None and shown.status == QualificationStatus.FAILED_RECIPE
    assert shown == failed

    # A complete published view without a status withdraws it: not yet verified.
    reader.snapshot = replace(reader.snapshot, items=(item,))
    assert library_status() is None


@pytest.mark.usefixtures(signed_recipe_releases.__name__)
def test_the_signed_index_status_reaches_the_library_and_a_bad_one_never_blocks(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    world = World(postgres_engine, tmp_path)
    entry = world.index["recipes"][0]
    working = _status(entry["content_sha256"], "Working")

    def publish(qualification: object) -> None:
        index = copy.deepcopy(world.index)
        index["recipes"][0]["qualification"] = qualification
        world.release.publish(index, world.packages)

    publish(working.model_dump(mode="json", exclude_none=True))
    world.sync()
    listed = world.library_recipe()
    assert listed.qualification == working
    # The detail (web detail, ``vonkctl recipe detail``) carries the same view.
    assert world.projection.recipe_detail(listed.selector).qualification == working

    # A recipe the published library no longer lists, imported by an earlier view.
    gone = replace(
        _item(_recipe(_model(), slug="deleted-upstream")), dependencies=(_model(),)
    )

    class Earlier:
        def list(self):
            return RecipeLibrarySnapshot("e" * 40, (gone,))

        def fetch(self, uri):
            return gone

    world.sync(Earlier())  # type: ignore[arg-type]

    # A status this Controller's contract cannot read (a newer catalog) reads
    # as not yet verified, never a sync problem: nothing waits on it.
    publish({"status": "Something newer", "required_sparks": 1})
    assert world.reader.list().problems == ()
    result = world.sync_service.sync(
        ManagedCatalogSyncRequest(
            request_key=str(uuid.uuid4()),
            trigger=CatalogSyncTrigger.AUTOMATIC,
            actor="test",
        )
    )
    assert result.state == CatalogSyncState.CURRENT
    assert result.withdrawn_count == 1
    # Not the last value kept forever, never Working: it reads as not yet verified.
    listed = world.library_recipe()
    detail = world.projection.recipe_detail(listed.selector)
    for shown in (listed.qualification, detail.qualification):
        assert shown is not None
        assert shown.status == QualificationStatus.NOT_YET_VERIFIED
