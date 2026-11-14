"""What an automatic sync applies is served at once, not one cadence step later."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine

from .signed_recipe_release import signed_recipe_releases
from .test_catalog_display_pass import World

pytestmark = pytest.mark.usefixtures(signed_recipe_releases.__name__)


def test_a_sync_that_adds_a_display_field_is_served_without_another_step(
    tmp_path: Path,
) -> None:
    # Wrong implementation: refreshing the library only before each sync serves
    # the displays from before a sync until the next step (2026-11-14: the v5.0.0
    # lineage was missing from the library for minutes after its sync).
    world = World(create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}"), tmp_path)
    world.index["catalog_entities"][0]["display"].pop("lineage")
    world.release.publish(world.index, world.packages)
    refresh = world.projection.refresh_catalog
    world.sync_service.advance_automatic(60, reconcile=refresh)
    (model,) = world.projection.models(limit=10).models
    assert model.model_display is not None and model.model_display.lineage is None

    # The next release (a new commit) adds the hierarchy to the Model's display.
    world.index["source_commit"] = "b" * 40
    world.publish(
        model={
            "lineage": {
                "family": "tiny",
                "line": "tiny",
                "model": "tiny",
                "variant_kind": "base",
            }
        }
    )
    world.sync_service.advance_automatic(60, reconcile=refresh)

    (model,) = world.projection.models(limit=10).models
    assert (model.family, model.line) == ("tiny", "tiny")
