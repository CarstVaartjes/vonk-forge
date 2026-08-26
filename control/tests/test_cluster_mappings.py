from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService, RecipeDraftInput
from vonk_control.cluster_mappings import ClusterMappingError, ClusterMappingService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import AgentNode, Base, ClusterMapping, ClusterMappingNode

from .test_catalog_service import _seed_recipe_dependencies


def setup(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mapping.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    node_ids = tuple("spk_" + f"{index:032x}" for index in (3, 1, 2))
    with sessions.begin() as session:
        session.add_all(
            AgentNode(
                node_id=node_id,
                state="active",
                architecture="linux-arm64",
                capabilities=["agent.runtime.rust.v1", "runtime.vonk.v1"],
            )
            for node_id in node_ids
        )
    inventory = InventoryRepository(sessions, clock=lambda: now)
    for index, node_id in enumerate(node_ids, start=1):
        inventory.record(
            InventorySnapshotInput(
                node_id=node_id,
                observed_at=now,
                disk_total_bytes=1_000,
                disk_free_bytes=900,
                host_memory_total_bytes=1_000,
                host_memory_free_bytes=900,
                gpu_memory_total_bytes=1_000,
                gpu_memory_free_bytes=900,
                gpu_count=1,
                artifact_store_read_only=False,
                capabilities=(
                    "runtime.vonk.v1",
                    "fabric.full_mesh.mbps.10000",
                ),
                fabric_address=f"192.168.100.{index}",
                fabric_bandwidth_mbps=10_000,
            )
        )
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    document["identity"]["slug"] = "glm-5-2-triple"
    document["artifacts"][0]["roles"] = ["entrypoint", "worker"]
    entrypoint = document["topology"]["roles"][0]
    document["topology"] = {
        "name": "triple-tp3",
        "mode": "tensor_parallel",
        "node_count": 3,
        "roles": [
            entrypoint,
            {
                **entrypoint,
                "name": "worker",
                "count": 2,
                "endpoint_owner": False,
            },
        ],
        "parallelism": {
            "world_size": 3,
            "tensor": 3,
            "pipeline": 1,
            "data": 1,
            "backend": "tcp",
        },
        "fabric": {"connectivity": "full_mesh", "minimum_bandwidth_mbps": 10000},
        "start_order": ["worker", "entrypoint"],
        "stop_order": ["entrypoint", "worker"],
    }
    document["parameters"] = [
        {
            "name": "max_model_len",
            "description": "Maximum context length.",
            "type": "integer",
            "default": 32768,
            "minimum": 1024,
            "maximum": 131072,
            "change_effect": "restart",
        }
    ]
    catalog = CatalogService(
        sessions, clock=lambda: now, cursors=TokenCodec(b"c" * 32).cursor_codec()
    )
    _seed_recipe_dependencies(catalog, document)
    draft = catalog.create_recipe(
        "admin", RecipeDraftInput(slug="glm-5-2-triple", document=document)
    )
    revision = catalog.resolve(draft.recipe_id, 1, "admin")
    return sessions, now, node_ids, revision


def test_three_node_topology_maps_deterministic_ranks(tmp_path: Path) -> None:
    sessions, now, node_ids, revision = setup(tmp_path)
    service = ClusterMappingService(sessions)

    plan = service.preview(
        revision.id,
        tuple(reversed(node_ids)),
        {},
        "admin",
    )

    assert [(node.rank, node.role) for node in plan.nodes] == [
        (0, "entrypoint"),
        (1, "worker"),
        (2, "worker"),
    ]
    assert [node.node_id for node in plan.nodes] == sorted(node_ids)
    mapping_id = service.materialize(plan, actor="admin", now=now)
    with sessions() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        nodes = tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == mapping_id)
                .order_by(ClusterMappingNode.rank)
            )
        )
        assert mapping is not None and mapping.topology_name == "triple-tp3"
        assert mapping.generation == 1
        assert [node.node_id for node in nodes] == sorted(node_ids)


def test_mapping_plan_binds_effective_parameters(tmp_path: Path) -> None:
    sessions, _now, node_ids, revision = setup(tmp_path)

    plan = ClusterMappingService(sessions).preview(
        revision.id,
        node_ids,
        {"max_model_len": 65536},
        "admin",
    )

    assert plan.parameters["max_model_len"] == 65536
    assert len(plan.placement_digest) == 64


def test_mapping_preview_is_actor_bound_and_ready_nodes_are_immutable(
    tmp_path: Path,
) -> None:
    sessions, now, node_ids, revision = setup(tmp_path)
    service = ClusterMappingService(sessions)
    assert not hasattr(service, "plan")
    with pytest.raises(ClusterMappingError) as caught:
        service.preview(revision.id, node_ids, {}, " " * 201)
    assert caught.value.code == "mapping.actor"
    mapping_id = service.materialize(
        service.preview(revision.id, node_ids, {}, "admin"), actor="admin", now=now
    )
    with (
        pytest.raises(ValueError, match="mapping.ready_immutable"),
        sessions.begin() as session,
    ):
        node = session.scalar(
            select(ClusterMappingNode).where(
                ClusterMappingNode.mapping_id == mapping_id
            )
        )
        assert node is not None
        node.role = "tampered"


def test_mapping_rejects_wrong_node_count_and_missing_required_fabric(
    tmp_path: Path,
) -> None:
    sessions, _now, node_ids, revision = setup(tmp_path)
    service = ClusterMappingService(sessions)

    with pytest.raises(ClusterMappingError) as caught:
        service.preview(revision.id, node_ids[:2], {}, "admin")
    assert caught.value.code == "mapping.node_count"

    InventoryRepository(sessions, clock=lambda: _now + timedelta(seconds=1)).record(
        InventorySnapshotInput(
            node_id=node_ids[0],
            observed_at=_now + timedelta(seconds=1),
            disk_total_bytes=1_000,
            disk_free_bytes=900,
            host_memory_total_bytes=1_000,
            host_memory_free_bytes=900,
            gpu_memory_total_bytes=1_000,
            gpu_memory_free_bytes=900,
            gpu_count=1,
            artifact_store_read_only=False,
            capabilities=("runtime.vonk.v1",),
        )
    )

    with pytest.raises(ClusterMappingError) as caught:
        service.preview(revision.id, node_ids, {}, "admin")
    assert caught.value.code == "topology.fabric_insufficient"


def test_mapping_does_not_trust_fabric_from_claim_capabilities(tmp_path: Path) -> None:
    sessions, now, node_ids, revision = setup(tmp_path)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_ids[0])
        assert node is not None
        node.capabilities = [
            "runtime.vonk.v1",
            "fabric.full_mesh.mbps.1000000",
        ]
    InventoryRepository(sessions, clock=lambda: now + timedelta(seconds=1)).record(
        InventorySnapshotInput(
            node_id=node_ids[0],
            observed_at=now + timedelta(seconds=1),
            disk_total_bytes=1_000,
            disk_free_bytes=900,
            host_memory_total_bytes=1_000,
            host_memory_free_bytes=900,
            gpu_memory_total_bytes=1_000,
            gpu_memory_free_bytes=900,
            gpu_count=1,
            artifact_store_read_only=False,
            capabilities=("runtime.vonk.v1",),
        )
    )

    with pytest.raises(ClusterMappingError) as caught:
        ClusterMappingService(sessions).preview(revision.id, node_ids, {}, "admin")

    assert caught.value.code == "topology.fabric_insufficient"


def test_mapping_rejects_forged_role_rank_and_endpoint_owner(tmp_path: Path) -> None:
    sessions, now, node_ids, revision = setup(tmp_path)
    service = ClusterMappingService(sessions)
    plan = service.preview(revision.id, node_ids, {}, "admin")
    forged_nodes = list(plan.nodes)
    forged_nodes[0] = replace(forged_nodes[0], role="worker", endpoint_owner=False)
    forged_nodes[1] = replace(forged_nodes[1], role="entrypoint", endpoint_owner=True)
    forged = replace(plan, nodes=tuple(forged_nodes))

    with pytest.raises(ClusterMappingError) as caught:
        service.materialize(forged, actor="admin", now=now)
    assert caught.value.code == "topology.role_mismatch"

    forged_nodes = list(plan.nodes)
    forged_nodes[0] = replace(forged_nodes[0], endpoint_owner=False)
    forged_nodes[1] = replace(forged_nodes[1], endpoint_owner=True)
    forged = replace(plan, nodes=tuple(forged_nodes))

    with pytest.raises(ClusterMappingError) as caught:
        service.materialize(forged, actor="admin", now=now)
    assert caught.value.code == "topology.role_mismatch"
