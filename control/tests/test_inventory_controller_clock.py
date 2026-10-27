"""Inventory freshness is the Controller's receive clock, never the agent's."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from vonk_control.inventory_repository import InventoryRepository
from vonk_control.models import (
    AgentCertificate,
    AgentNodeContact,
    NodeInventorySnapshot,
)
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.run_switch_operations.planning_helpers import PlanInventory
from vonk_control.unused_storage_collection.references import _latest_snapshots

from .test_agent_api import NODE_A, agent_headers, make_agent_system
from .test_install_admission import _service
from .test_install_admission import setup as install_setup
from .test_recipe_builds import setup as build_setup

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


SLOW = timedelta(minutes=10)


def _feed_through_the_endpoint(tmp_path: Path, sessions, node_id, now, payload) -> None:
    """Replace the world's inventory by one report from an agent whose clock
    runs 10 minutes slow, accepted by the real `/agent/inventory` route."""
    with sessions.begin() as session:
        session.execute(delete(NodeInventorySnapshot))
        found = session.execute(
            select(AgentCertificate.serial, AgentCertificate.fingerprint).where(
                AgentCertificate.node_id == node_id
            )
        ).first()
        serial, fingerprint = found if found else (None, None)
        if session.get(AgentNodeContact, node_id) is None:
            session.add(AgentNodeContact(node_id=node_id, last_seen_at=now))
        if serial is None:
            serial, fingerprint = "serial-clock", "fingerprint-serial-clock"
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    fingerprint=fingerprint,
                    not_before=now - timedelta(seconds=1),
                    not_after=now + timedelta(days=1),
                )
            )
    (tmp_path / "agent").mkdir()
    client, _services, _codec, clock = make_agent_system(
        tmp_path / "agent", engine=sessions.kw["bind"]
    )
    clock.now = now
    response = client.post(
        "/agent/inventory",
        headers={
            **agent_headers(node_id, serial),
            "x-vonk-agent-fingerprint": fingerprint,
        },
        json={
            "schema_version": 1,
            "observed_at": (now - SLOW).isoformat(),
            "gpu_count": 1,
            "artifact_store_read_only": False,
            "nvidia_driver_version": "unknown",
            "container_runtime_version": "unknown",
            **payload,
        },
    )
    assert response.status_code == 204, response.text


def test_a_slow_agent_clock_is_admitted_for_install_and_run_switch(tmp_path) -> None:
    """Catches inventory aged by the agent stamp: a Spark whose clock runs 10
    minutes slow was refused for installs and Run/Switch plans indefinitely."""
    sessions, now, node, mapping, build = install_setup(tmp_path / "install")
    _feed_through_the_endpoint(
        tmp_path,
        sessions,
        node,
        now,
        {
            "disk_total_bytes": 1000,
            "disk_free_bytes": 200,
            "host_memory_total_bytes": 1000,
            "host_memory_free_bytes": 800,
            "gpu_memory_total_bytes": 1000,
            "gpu_memory_free_bytes": 800,
            "capabilities": ["runtime.vonk.v1", "recipe.image.pull.v1"],
            "memory_pool": "shared",
        },
    )

    plan = _service(sessions, inventory_max_age=300, disk_floor_bytes=10).plan_install(
        mapping, build, now=now
    )
    assert plan.allowed
    with sessions() as session:
        _snapshot, evidence = PlanInventory(
            session, now=now, maximum_age_seconds=300
        ).read(node)
    assert evidence.state == "fresh"


def test_a_slow_agent_clock_is_admitted_for_a_recipe_build(tmp_path) -> None:
    (tmp_path / "build").mkdir()
    sessions, bundles, now, node, revision = build_setup(tmp_path / "build")
    _feed_through_the_endpoint(
        tmp_path,
        sessions,
        node,
        now,
        {
            "disk_total_bytes": 2 * 1024**4,
            "disk_free_bytes": 1 * 1024**4,
            "host_memory_total_bytes": 100_000,
            "host_memory_free_bytes": 80_000,
            "gpu_memory_total_bytes": 100_000,
            "gpu_memory_free_bytes": 80_000,
            "capabilities": ["recipe.build.v1", "recipe.build.egress-proxy.v1"],
            "memory_pool": "separate",
        },
    )

    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node, now=now
    )
    assert plan.build_id
