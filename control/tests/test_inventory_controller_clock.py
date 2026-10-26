"""Inventory freshness is the Controller's receive clock, never the agent's."""

from __future__ import annotations

from datetime import timedelta

import pytest
from vonk_control.inventory_repository import InventoryRepository
from vonk_control.unused_storage_collection.references import _latest_snapshots

from .test_agent_api import NODE_A, agent_headers, make_agent_system

MAX_AGE = 300


@pytest.fixture
def agent_system(tmp_path):
    return make_agent_system(tmp_path)


def _post(client, clock, *, collected_ago: timedelta) -> None:
    payload = {
        "schema_version": 1,
        "observed_at": (clock.now - collected_ago).isoformat(),
        "disk_total_bytes": 1000,
        "disk_free_bytes": 700,
        "host_memory_total_bytes": 2000,
        "host_memory_free_bytes": 1500,
        "gpu_memory_total_bytes": 1000,
        "gpu_memory_free_bytes": 800,
        "gpu_count": 1,
        "memory_pool": "separate",
        "artifact_store_read_only": False,
        "capabilities": ["runtime.vonk.v1", "recipe.image.pull.v1"],
        "nvidia_driver_version": "580.65.06",
        "container_runtime_version": "28.3.3",
    }
    response = client.post(
        "/agent/inventory", headers=agent_headers(NODE_A, "serial-a"), json=payload
    )
    assert response.status_code == 204, response.text


@pytest.mark.parametrize(
    "collected_ago", (timedelta(minutes=6), timedelta(minutes=10)), ids=("6m", "10m")
)
def test_report_received_now_is_fresh_whatever_the_agent_stamp(
    agent_system, collected_ago
) -> None:
    """Catches ageing by the agent stamp: a slow clock or an early collection
    makes every fresh report stale, so installs and builds are refused forever
    (install admission and builder selection read exactly this `stale`)."""
    client, services, _codec, clock = agent_system
    _post(client, clock, collected_ago=collected_ago)

    view = InventoryRepository(services.sessions).latest(
        NODE_A, now=clock.now, maximum_age=MAX_AGE
    )
    assert not view.stale
    with services.sessions() as session:
        _free, _total, received = _latest_snapshots(session)[NODE_A]
    assert clock.now - received < timedelta(seconds=MAX_AGE)


def test_report_received_long_ago_is_stale(agent_system) -> None:
    client, services, _codec, clock = agent_system
    _post(client, clock, collected_ago=timedelta(0))
    clock.now += timedelta(minutes=6)

    view = InventoryRepository(services.sessions).latest(
        NODE_A, now=clock.now, maximum_age=MAX_AGE
    )
    assert view.stale
    with services.sessions() as session:
        _free, _total, received = _latest_snapshots(session)[NODE_A]
    assert clock.now - received > timedelta(seconds=MAX_AGE)
