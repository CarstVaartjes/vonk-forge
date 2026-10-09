"""The lane's sibling recipes are two real Recipes that build one image."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from .recipe_library_source import recipe_library_root

ROOT = recipe_library_root()
REPOSITORY = Path(__file__).resolve().parents[2]

SCRIPT = """
import json
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

os.environ["VONK_SYNTHETIC_CANARY_INDEX"] = (
    "tests/fixtures/canonical-synthetic-canary/index.json"
)
lifecycle = runpy.run_path("tests/acceptance/test_spark_lifecycle.py")
importer = runpy.run_path("tests/acceptance/spark_canary_catalog_import.py")
canary = lifecycle["_canonical_canary_fixture"](Path(sys.argv[1]))
one, two = lifecycle["_sibling_recipes"](canary)

# Different Recipes: another slug and another revision content.
assert one.slug != two.slug and {one.slug, two.slug}.isdisjoint({canary.slug})
assert one.recipe_content_sha256 != two.recipe_content_sha256
# One image: everything that builds and runs it is the canary's.
# The canary service answers only to its own model name, so the interfaces
# (model aliases) are the canary's too; the route alias is the assignment name.
for part in ("execution", "runtime", "models", "topology", "interfaces"):
    assert one.recipe[part] == two.recipe[part] == canary.recipe[part], part

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
index = json.loads(one.index_bytes)
assert one.index_bytes == two.index_bytes and len(index["recipes"]) == 2
archives = {}
for number, sibling in enumerate((one, two)):
    package = work / f"{number}.tar.gz"
    package.write_bytes(sibling.package_bytes)
    archives[
        next(
            entry["package"]["sha256"]
            for entry in index["recipes"]
            if entry["content_sha256"] == sibling.recipe_content_sha256
        )
    ] = package
reader = importer["FixtureReader"](index, archives)
view = ManagedRecipeCatalogSyncService(
    sessions, catalog=catalog, reader=reader, clock=clock
).sync(
    request_key="00000000-0000-4000-8000-000000000001",
    trigger="manual",
    actor="test",
    reviewed_snapshot=reader.snapshot,
)
assert view.state == "current", view
assert not view.problems, view
with sessions() as session:
    recipes = session.scalars(
        select(CatalogDocumentRevision).where(CatalogDocumentRevision.kind == "recipe")
    ).all()
assert {row.slug for row in recipes} == {one.slug, two.slug}, recipes
"""


@pytest.mark.needs_recipe_library
def test_sibling_recipes_sync_as_two_recipes_that_share_one_image_definition() -> None:
    subprocess.run(
        [sys.executable, "-c", SCRIPT, str(ROOT)],
        cwd=REPOSITORY,
        check=True,
    )


REMOVAL = """
import runpy

from vonk_control.run_switch_contract import (
    RunSwitchCleanupVerifyResult,
    RunSwitchUninstallResult,
)

removed_by = runpy.run_path("tests/acceptance/test_spark_lifecycle.py")[
    "_installations_removed"
]
first = "11111111-1111-4111-8111-111111111111"
second = "22222222-2222-4222-8222-222222222222"


def removed(installation, verified=True):
    return [
        RunSwitchUninstallResult.model_construct(installation_id=installation),
        RunSwitchCleanupVerifyResult.model_construct(
            installation_id=installation,
            final_verified=verified,
            removed=True,
            active_runs=0,
            installation_state="uninstalled",
        ),
    ]


assert removed_by(removed(first) + removed(second), [first, second])
# A cleanup that proves only the last installation does not prove the first.
assert not removed_by(removed(second), [first, second])
assert not removed_by(removed(first) + removed(second, False), [first, second])
"""


def test_a_cleanup_needs_removal_receipts_for_every_installation_it_names() -> None:
    subprocess.run([sys.executable, "-c", REMOVAL], cwd=REPOSITORY, check=True)
