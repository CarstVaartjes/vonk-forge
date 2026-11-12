"""The Controller keeps the words of the release it synced and serves them.

The dashboard and vonkctl render capability labels and status meanings from
this one stored copy, never from a hand copy of their own.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update
from vonk_control.auth import Actor, TokenCodec
from vonk_control.library_api import install_library_routes
from vonk_control.library_projection import LibraryProjection
from vonk_control.models import RecipeLibrarySyncRun
from vonk_forge_contracts import catalog_vocabulary

from .signed_recipe_release import signed_recipe_releases
from .test_catalog_display_pass import World

_URL = "/api/library/vocabulary"


def _client(projection: LibraryProjection) -> TestClient:
    app = FastAPI()
    install_library_routes(
        app,
        actor_dependency=Depends(lambda: Actor("viewer", "viewer")),
        projection=projection,
    )
    return TestClient(app)


@pytest.mark.usefixtures(signed_recipe_releases.__name__)
def test_sync_stores_the_release_vocabulary_and_a_newer_release_replaces_it(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    world = World(postgres_engine, tmp_path)
    client = _client(world.projection)

    # Before the first sync there is nothing to serve (every release carries one).
    assert client.get(_URL).status_code == 404

    published = catalog_vocabulary().model_dump(mode="json")
    index = copy.deepcopy(world.index)
    index["vocabulary"] = published
    world.release.publish(index, world.packages)
    world.sync()
    assert client.get(_URL).json() == published

    # A newer release replaces it.
    newer = copy.deepcopy(published)
    newer["capabilities"][0]["label"] = "A newer label"
    index["vocabulary"] = newer
    world.release.publish(index, world.packages)
    world.sync()
    assert client.get(_URL).json() == newer

    # A restart serves the stored copy with no sync.
    restarted = LibraryProjection(
        world.sessions, cursors=TokenCodec(b"d" * 32).cursor_codec()
    )
    assert _client(restarted).get(_URL).json() == newer


@pytest.mark.usefixtures(signed_recipe_releases.__name__)
def test_a_tick_on_an_unchanged_release_writes_a_missing_vocabulary(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    world = World(postgres_engine, tmp_path)
    index = copy.deepcopy(world.index)
    index["vocabulary"] = catalog_vocabulary().model_dump(mode="json")
    world.release.publish(index, world.packages)
    world.sync()
    # A Controller upgraded onto the same release: same commit, no vocabulary yet.
    with world.sessions.begin() as session:
        session.execute(update(RecipeLibrarySyncRun).values(vocabulary=None))
    assert world.projection.library_vocabulary() is None

    world.sync_service.automatic()
    assert world.projection.library_vocabulary() == catalog_vocabulary()
