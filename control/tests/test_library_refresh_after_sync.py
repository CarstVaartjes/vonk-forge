"""What the worker's catalog sync applies is served at once, not one cadence step later."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import create_engine

from vonk_agent_protocol.capability_reply import (
    CapabilityReason,
    CapabilityUnavailableReply,
    ControllerCapability,
)
from vonk_control.api.production import LibraryObservations, observe_default_key
from vonk_control.capabilities import CapabilityUnavailableError
from vonk_control.gateway.keys import GatewayKeyService, keep_default_key

from .signed_recipe_release import signed_recipe_releases
from .test_catalog_display_pass import World
from .test_gateway_keys import MASTER, FakeLiteLlm


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Counted:
    """A library whose refreshes are counted; the first ``failures`` raise."""

    def __init__(self, library=None, *, failures: int = 0) -> None:
        self._library = library
        self._failures = failures
        self.refreshes = 0

    def refresh_catalog(self) -> None:
        self.refreshes += 1
        if self.refreshes <= self._failures:
            raise ValueError("unreadable catalog row")
        if self._library is not None:
            self._library.refresh_catalog()


@pytest.mark.usefixtures(signed_recipe_releases.__name__)
def test_a_sync_that_adds_a_display_field_is_served_without_another_step(
    tmp_path: Path,
) -> None:
    # Wrong implementation: refreshing the library only on the full cadence
    # serves the displays from before a sync until the next step (2026-11-14:
    # the v5.0.0 lineage was missing from the library for minutes after its sync).
    world = World(create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}"), tmp_path)
    world.index["catalog_entities"][0]["display"].pop("lineage")
    world.release.publish(world.index, world.packages)
    library = _Counted(world.projection)
    api = LibraryObservations(
        library,  # type: ignore[arg-type]
        world.sync_service.last_completed_at,
        interval_seconds=60,
        monotonic=_Clock(),
    )
    world.sync_service.advance_automatic(60)  # the worker
    api.tick()
    (model,) = world.projection.models(limit=10).models
    assert model.model_display is not None
    assert model.model_display.lineage is None
    # No new sync and no interval passed: nothing is read again.
    api.tick()
    assert library.refreshes == 1

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


def test_a_failed_library_read_waits_for_the_next_sync_or_interval(caplog) -> None:
    """Catches a failing read retried every tick, or the only owner ending on it."""
    library = _Counted(failures=1)
    clock = _Clock()
    marker = ["first sync"]
    api = LibraryObservations(
        library,  # type: ignore[arg-type]
        lambda: marker[0],
        interval_seconds=60,
        monotonic=clock,
    )
    with caplog.at_level(logging.WARNING):
        api.tick()
    assert library.refreshes == 1
    assert "library observation unavailable: ValueError" in caplog.text
    clock.now += 5
    api.tick()  # the same sync, inside the interval: no retry
    assert library.refreshes == 1
    marker[0] = "next sync"
    api.tick()
    assert library.refreshes == 2
    clock.now += 60
    api.tick()
    assert library.refreshes == 3


def test_an_unavailable_default_key_logs_its_cause(caplog) -> None:
    """Catches a log line that names only the exception type."""

    def unavailable():
        raise CapabilityUnavailableError(
            CapabilityUnavailableReply(
                capability=ControllerCapability.GATEWAY_KEYS,
                reason=CapabilityReason.DEPENDENCY_UNAVAILABLE,
            )
        )

    with caplog.at_level(logging.WARNING):
        observe_default_key(unavailable)
    assert "default gateway key observation unavailable" in caplog.text
    assert "capability=gateway-keys" in caplog.text


def test_an_ended_default_key_observation_heals_on_the_next_periodic_one(
    tmp_path: Path,
) -> None:
    """Catches the default key staying missing once the startup observation gives up."""
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
    observe_default_key(lambda: gateway.ensure_default(path))  # still offline
    assert not peer.keys
    ready[0] = True
    observe_default_key(lambda: gateway.ensure_default(path))
    assert peer.keys["default"]["key"] == path.read_text().strip()
