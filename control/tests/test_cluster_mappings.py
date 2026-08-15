from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from test_catalog_service import _seed_recipe_dependencies
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService, RecipeDraftInput
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.models import AgentNode, Base, ClusterMapping, ClusterMappingNode


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
                capabilities=["runtime.vonk.v1"],
            )
            for node_id in node_ids
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
        "parallelism": {"tensor": 3, "pipeline": 1, "data": 1, "backend": "tcp"},
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

    plan = service.plan(
        revision.id,
        tuple(reversed(node_ids)),
        parameters={},
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

    plan = ClusterMappingService(sessions).plan(
        revision.id,
        node_ids,
        parameters={"max_model_len": 65536},
    )

    assert plan.parameters["max_model_len"] == 65536
    assert len(plan.placement_digest) == 64
