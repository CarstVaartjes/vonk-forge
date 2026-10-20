from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from vonk_agent_protocol import AgentEvidenceCode
from vonk_control.agent_api import InventoryRequest
from vonk_control.api import create_app
from vonk_control.auth import Actor
from vonk_control.cache_removal_review import CacheRemovalReview
from vonk_control.inventory_repository import InventoryRepository
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import AgentCertificate
from vonk_control.run_admission import RunAdmissionService, require_admissible

from .test_agent_api import NODE_A, Jobs, agent_headers, make_agent_system
from .test_model_removal_worker_postgres import _prepare_removal_target
from .test_run_admission import setup
from .wire_probes import prebuilt_probe


@pytest.fixture
def agent_system(tmp_path, postgres_engine):
    return make_agent_system(tmp_path, engine=postgres_engine)


@pytest.fixture(scope="session")
def inventory_wire_probe() -> Path:
    return prebuilt_probe("VONK_INVENTORY_WIRE_PROBE")


@pytest.fixture
def inventory_request(inventory_wire_probe: Path) -> InventoryRequest:
    # The producer crosses the actual collector subprocess before HTTP. The
    # old 64-KiB reader rejects this valid report before the Controller sees it.
    completed = subprocess.run(
        [str(inventory_wire_probe)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert len(completed.stdout) > 64 * 1024
    request = InventoryRequest.model_validate_json(completed.stdout)
    assert request.model_objects is not None and len(request.model_objects) == 1024
    return request


def test_rust_inventory_json_crosses_controller_route_and_admission(
    inventory_request: InventoryRequest, agent_system, postgres_engine, tmp_path
) -> None:
    """Catches model-object evidence starving capacity before or after ingest."""
    client, services, _codec, clock = agent_system
    _sessions, now, node, installation = setup(
        tmp_path, engine=postgres_engine, memory_pool=inventory_request.memory_pool
    )
    clock.now = now + timedelta(minutes=6)
    admission = RunAdmissionService(services.sessions, memory_floor_bytes=0)
    assert not admission.plan_run(installation, alias="qwen", now=clock.now).allowed
    with services.sessions.begin() as session:
        session.add(
            AgentCertificate(
                serial="serial-inventory",
                node_id=node,
                fingerprint="fingerprint-serial-inventory",
                not_before=now,
                not_after=clock.now + timedelta(hours=1),
            )
        )
    request = inventory_request.model_copy(update={"observed_at": clock.now})
    response = client.post(
        "/agent/inventory",
        json=request.model_dump(mode="json"),
        headers=agent_headers(node, "serial-inventory"),
    )
    assert response.status_code == 204, response.text
    stored = InventoryRepository(services.sessions).latest(
        node, now=clock.now, maximum_age=60
    )
    assert stored.disk_free_bytes == request.disk_free_bytes
    assert stored.memory_pool == request.memory_pool
    assert not stored.stale
    assert stored.model_objects == request.model_objects
    require_admissible(admission.plan_run(installation, alias="qwen", now=clock.now))

    malformed = request.model_dump(mode="json")
    malformed["gpu_count"] = "1"
    assert (
        client.post(
            "/agent/inventory",
            json=malformed,
            headers=agent_headers(node, "serial-inventory"),
        ).status_code
        == 422
    )


def test_rust_inventory_objects_reach_fleet_removal_review(
    inventory_request: InventoryRequest, agent_system, tmp_path
) -> None:
    """Catches a writer/reader mismatch or unknown inventory treated as empty."""
    client, services, codec, clock = agent_system
    request = inventory_request.model_copy(update={"observed_at": clock.now})
    assert (
        client.post(
            "/agent/inventory",
            json=request.model_dump(mode="json"),
            headers=agent_headers(NODE_A, "serial-a"),
        ).status_code
        == 204
    )
    cache = ModelCacheService(
        services.sessions,
        tmp_path / "nas",
        reserve_bytes=0,
        fixture_sources=True,
        clock=clock,
    )
    try:
        selector, _digest, path = _prepare_removal_target(
            cache,
            services.sessions,
            tmp_path,
            slug="inventory-vector",
            data=b"inventory weights",
        )
        app = create_app(jobs=Jobs(), tokens=codec, now=lambda: 0, model_cache=cache)
        response = TestClient(app).get(
            f"/api/model/{selector}/remove-review",
            params={"fleet": True},
            headers={
                "authorization": f"Bearer {codec.issue(Actor('operator', 'administrator'), now=0, ttl_seconds=60)}"
            },
        )
        assert response.status_code == 200, response.text
        review = CacheRemovalReview.model_validate_json(response.content)
        assert review.fleet is not None
        target = next(item for item in review.fleet.targets if item.node_id == NODE_A)
        assert target.observation_available
        assert [item.object.sha256 for item in target.objects] == [path.name]
        assert target.reclaimable_bytes == len(b"inventory weights")
    finally:
        cache.close()


def test_optional_model_evidence_damage_keeps_core_and_fresh_report_recovers(
    inventory_request: InventoryRequest, agent_system, caplog
) -> None:
    """Catches optional stored evidence raising before tolerant wire validation."""
    client, services, _codec, clock = agent_system
    caplog.set_level("INFO")
    request = inventory_request.model_copy(update={"observed_at": clock.now})
    payload = request.model_dump(mode="json")
    payload["model_objects"] = 42
    assert (
        client.post(
            "/agent/inventory", json=payload, headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 204
    )
    repo = InventoryRepository(services.sessions)
    stored = repo.latest(NODE_A, now=clock.now, maximum_age=60)
    assert not stored.stale and stored.model_objects is None
    # Damage only optional evidence through the actual PostgreSQL storage seam.
    with services.sessions.begin() as session:
        session.execute(
            text(
                "UPDATE node_inventory_snapshots SET model_objects = '42'::json WHERE id = :id"
            ),
            {"id": stored.id},
        )
    damaged = repo.latest(NODE_A, now=clock.now, maximum_age=60)
    assert damaged.disk_free_bytes == request.disk_free_bytes
    assert not damaged.stale and damaged.model_objects is None
    assert any(
        json.loads(record.getMessage())["code"]
        == AgentEvidenceCode.INVENTORY_MODEL_OBJECTS_DROPPED.value
        for record in caplog.records
        if record.name == "vonk_control.inventory_repository"
    )
    clock.now += timedelta(seconds=1)
    payload = request.model_copy(update={"observed_at": clock.now}).model_dump(
        mode="json"
    )
    assert (
        client.post(
            "/agent/inventory", json=payload, headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 204
    )
    assert (
        repo.latest(NODE_A, now=clock.now, maximum_age=60).model_objects
        == request.model_objects
    )
