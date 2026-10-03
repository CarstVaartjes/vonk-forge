"""The acceptance lane's editorial successor is a real second revision."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from .recipe_library_source import recipe_library_root

ROOT = recipe_library_root()
REPOSITORY = Path(__file__).resolve().parents[2]

SCRIPT = """
import os
import runpy
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.models import Base, CatalogDocumentRevision
from vonk_control.source_bundles import SourceBundleStore

import json

os.environ["VONK_SYNTHETIC_CANARY_INDEX"] = (
    "tests/fixtures/canonical-synthetic-canary/index.json"
)
lifecycle = runpy.run_path("tests/acceptance/test_spark_lifecycle.py")
importer = runpy.run_path("tests/acceptance/spark_canary_catalog_import.py")
first = lifecycle["_canonical_canary_fixture"](Path(sys.argv[1]))
successor = lifecycle["_editorial_successor"](first)
assert successor.recipe_content_sha256 != first.recipe_content_sha256
assert successor.source_commit != first.source_commit

work = Path(tempfile.mkdtemp())
engine = create_engine(f"sqlite:///{work / 'catalog.sqlite'}")
Base.metadata.create_all(engine)
sessions = sessionmaker(engine, expire_on_commit=False)


def clock():
    return datetime(2026, 9, 5, tzinfo=UTC)


catalog = CatalogService(
    sessions,
    clock=clock,
    cursors=TokenCodec(b"s" * 32).cursor_codec(),
    source_bundles=SourceBundleStore(work / "bundles"),
)
for number, fixture in enumerate((first, successor), start=1):
    package = work / f"{number}.tar.gz"
    package.write_bytes(fixture.package_bytes)
    reader = importer["FixtureReader"](
        json.loads(fixture.index_bytes),
        {json.loads(fixture.index_bytes)["recipes"][0]["package"]["sha256"]: package},
    )
    view = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=reader, clock=clock
    ).sync(
        request_key=f"00000000-0000-4000-8000-00000000000{number}",
        trigger="manual",
        actor="test",
        expected_commit=reader.snapshot.commit,
    )
    assert view.state == "current", view
    assert not view.problems, view
with sessions() as session:
    recipes = session.scalars(
        select(CatalogDocumentRevision)
        .where(CatalogDocumentRevision.kind == "recipe")
        .order_by(CatalogDocumentRevision.revision_number)
    ).all()
assert [row.revision_number for row in recipes] == [1, 2], recipes
assert recipes[0].slug == recipes[1].slug == first.slug
assert recipes[1].content_digest == successor.recipe_content_sha256
"""


@pytest.mark.needs_recipe_library
def test_the_canary_successor_syncs_as_a_second_revision_of_the_same_recipe() -> None:
    subprocess.run(
        [sys.executable, "-c", SCRIPT, str(ROOT)],
        cwd=REPOSITORY,
        check=True,
    )
