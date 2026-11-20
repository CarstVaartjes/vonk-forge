"""What the worker's catalog sync applies is served at once, not one cadence step later."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import create_engine
from vonk_control.api.production import LibraryObservations
from vonk_control.gateway_keys import GatewayKeyService, keep_default_key

from .signed_recipe_release import signed_recipe_releases
from .test_catalog_display_pass import World
from .test_gateway_keys import MASTER, FakeLiteLlm

pytestmark = pytest.mark.usefixtures(signed_recipe_releases.__name__)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _ReadyKey:
    def __init__(self) -> None:
        self.checks = 0

    def ensure_default(self) -> bool:
        self.checks += 1
        return True


def test_a_sync_that_adds_a_display_field_is_served_without_another_step(
    tmp_path: Path,
) -> None:
    # Wrong implementation: refreshing the library only on the full cadence
    # serves the displays from before a sync until the next step (2026-11-14:
    # the v5.0.0 lineage was missing from the library for minutes after its sync).
    world = World(create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}"), tmp_path)
    world.index["catalog_entities"][0]["display"].pop("lineage")
    world.release.publish(world.index, world.packages)
    keys = _ReadyKey()
    api = LibraryObservations(
        world.projection,
        keys.ensure_default,
        world.sync_service.last_completed_at,
        interval_seconds=60,
        monotonic=_Clock(),
    )
    world.sync_service.advance_automatic(60)  # the worker
    api.tick()
    (model,) = world.projection.models(limit=10).models
    assert model.model_display is not None and model.model_display.lineage is None
    # No new sync and no interval passed: nothing is read again.
    api.tick()
    assert keys.checks == 1

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
    world.sync_service.advance_automatic(60)
    api.tick()

    (model,) = world.projection.models(limit=10).models
    assert (model.family, model.line) == ("tiny", "tiny")


def test_an_ended_default_key_observation_heals_on_the_next_full_observation(
    tmp_path: Path,
) -> None:
    """Catches the default key staying missing once the startup observation gives up."""
    world = World(create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}"), tmp_path)
    peer = FakeLiteLlm()
    ready = [False]

    def transport(request):
        if not ready[0]:
            raise httpx2.ReadError("offline", request=request)
        return peer.handle(request)

    gateway = GatewayKeyService(
        master_key=lambda: MASTER,
        transport=httpx2.MockTransport(transport),
        intent_root=tmp_path / "mutations",
    )
    path = tmp_path / "client-key"
    asyncio.run(
        asyncio.wait_for(
            keep_default_key(
                gateway,
                asyncio.Event(),
                path=path,
                first_delay=0.001,
                maximum_delay=0.001,
            ),
            timeout=10,
        )
    )
    clock = _Clock()
    api = LibraryObservations(
        world.projection,
        lambda: gateway.ensure_default(path),
        world.sync_service.last_completed_at,
        interval_seconds=60,
        monotonic=clock,
    )
    api.tick()  # the gateway is still offline: logged, asked again later
    assert not peer.keys
    ready[0] = True
    api.tick()
    assert not peer.keys  # not before the interval
    clock.now += 60
    api.tick()
    assert peer.keys["default"]["key"] == path.read_text().strip()


def test_an_unexpected_refresh_failure_leaves_the_observation_running() -> None:
    """Catches the only library owner ending on an error it did not list."""

    class Library:
        def __init__(self) -> None:
            self.refreshes = 0

        def refresh_catalog(self) -> None:
            self.refreshes += 1
            if self.refreshes == 1:
                raise ValueError("unreadable catalog row")

    library = Library()
    keys = _ReadyKey()
    clock = _Clock()
    api = LibraryObservations(
        library,  # type: ignore[arg-type]
        keys.ensure_default,
        lambda: None,
        interval_seconds=60,
        monotonic=clock,
    )
    api.tick()
    clock.now += 60
    api.tick()
    assert library.refreshes == 2 and keys.checks == 1
