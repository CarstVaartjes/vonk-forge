"""Physical memory evidence survives the authenticated API and PostgreSQL."""

import pytest
from vonk_control.inventory_repository import InventoryRepository

from .test_agent_api import NODE_A, agent_headers, make_agent_system


def _payload(clock):
    return {
        "schema_version": 1,
        "observed_at": clock.now.isoformat(),
        "disk_total_bytes": 1000,
        "disk_free_bytes": 700,
        "host_memory_total_bytes": 2000,
        "host_memory_free_bytes": 1500,
        "gpu_memory_total_bytes": 2000,
        "gpu_memory_free_bytes": 1500,
        "gpu_count": 1,
        "memory_pool": "shared",
        "artifact_store_read_only": False,
        "capabilities": ["runtime.vonk.v1"],
        "nvidia_driver_version": "580.65.06",
        "container_runtime_version": "28.3.3",
    }


@pytest.mark.parametrize("pool", ["shared", "separate"])
def test_authenticated_memory_pool_is_preserved_without_inferring_from_counts(
    tmp_path, postgres_engine, pool
):
    client, services, _, clock = make_agent_system(tmp_path, engine=postgres_engine)
    payload = _payload(clock) | {"memory_pool": pool}
    response = client.post(
        "/agent/inventory", headers=agent_headers(NODE_A, "serial-a"), json=payload
    )
    assert response.status_code == 204, response.text
    stored = InventoryRepository(services.sessions).latest(
        NODE_A, now=clock.now, maximum_age=60
    )
    # Identical totals can describe either hardware layout. This assertion
    # detects dropping the field, guessing from totals, or swapping its meaning.
    assert stored.memory_pool == pool


@pytest.mark.parametrize("invalid", ["omitted", "null", "unknown", "no-gpu"])
def test_unproved_or_contradictory_memory_pool_cannot_be_recorded(
    tmp_path, postgres_engine, invalid
):
    client, services, _, clock = make_agent_system(tmp_path, engine=postgres_engine)
    payload = _payload(clock)
    if invalid == "omitted":
        del payload["memory_pool"]
    elif invalid == "null":
        payload["memory_pool"] = None
    elif invalid == "unknown":
        payload["memory_pool"] = "unknown"
    else:
        payload["gpu_count"] = 0
    response = client.post(
        "/agent/inventory", headers=agent_headers(NODE_A, "serial-a"), json=payload
    )
    assert response.status_code == 422, response.text
    assert InventoryRepository(services.sessions).snapshot_count(NODE_A) == 0
