from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.cluster_mappings import ClusterMappingError, ClusterMappingService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    ClusterMapping,
    ClusterMappingNode,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

MODEL_DOCUMENT_ID = "00000000-0000-4000-8000-000000000010"
MODEL_REVISION_ID = "00000000-0000-4000-8000-000000000011"
RECIPE_DOCUMENT_ID = "00000000-0000-4000-8000-000000000020"
RECIPE_REVISION_ID = "00000000-0000-4000-8000-000000000021"


RECIPE_OPTIONS = [
    {
        "name": "verification",
        "label": "Verification",
        "help": "How drafted tokens are verified.",
        "choices": [
            {
                "value": "standard",
                "label": "Standard",
                "help": "Verify every token.",
                "default": True,
            },
            {
                "value": "adaptive",
                "label": "Adaptive",
                "help": "Verify a prefix.",
                "env": {"VERIFY_MODE": "adaptive"},
            },
        ],
    }
]


def _canonical_catalog_documents(
    *, options: bool = False
) -> tuple[ModelDefinition, RecipeDefinition]:
    model = ModelDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "model-definition.json")
            .read_text(encoding="utf-8")
        )
    )
    raw_recipe = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )
    raw_recipe["identity"]["slug"] = "glm-5-2-triple"
    if options:
        raw_recipe["options"] = copy.deepcopy(RECIPE_OPTIONS)
    raw_recipe["settings"]["knobs"]["max_model_len"] = {
        "value": 32768,
        "change_effect": "restart",
    }
    entrypoint = raw_recipe["topology"]["roles"][0]
    worker = copy.deepcopy(entrypoint)
    worker.update({"name": "worker", "count": 2, "endpoint_owner": False})
    raw_recipe["topology"] = {
        "name": "triple-tp3",
        "node_count": 3,
        "roles": [entrypoint, worker],
        "parallelism": {
            "tensor": 3,
            "pipeline": 1,
            "data": 1,
            "backend": "tcp",
        },
        "start_order": ["worker", "entrypoint"],
    }
    return model, RecipeDefinition.model_validate(raw_recipe)


def _seed_canonical_catalog(
    sessions: sessionmaker, now: datetime, *, options: bool = False
) -> CatalogDocumentRevision:
    model, recipe = _canonical_catalog_documents(options=options)
    model_digest = document_sha256(model.model_dump(mode="json"))
    recipe_digest = document_sha256(recipe.model_dump(mode="json"))
    with sessions.begin() as session:
        session.add_all(
            [
                CatalogDocument(
                    id=MODEL_DOCUMENT_ID,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    title=model.identity.model.title,
                    created_by="test",
                    created_at=now,
                    updated_at=now,
                ),
                CatalogDocument(
                    id=RECIPE_DOCUMENT_ID,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    title=recipe.metadata.title,
                    created_by="test",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.add_all(
            [
                CatalogDocumentRevision(
                    id=MODEL_REVISION_ID,
                    document_id=MODEL_DOCUMENT_ID,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=model.model_dump(mode="json"),
                    content_digest=model_digest,
                    artifact_key="a" * 64,
                    created_by="test",
                    created_at=now,
                ),
                CatalogDocumentRevision(
                    id=RECIPE_REVISION_ID,
                    document_id=RECIPE_DOCUMENT_ID,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=recipe.model_dump(mode="json"),
                    content_digest=recipe_digest,
                    execution_key="b" * 64,
                    created_by="test",
                    created_at=now,
                ),
            ]
        )
        session.add_all(
            [
                CatalogDocumentHead(
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    active_revision_id=MODEL_REVISION_ID,
                    generation=1,
                ),
                CatalogDocumentHead(
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    active_revision_id=RECIPE_REVISION_ID,
                    generation=1,
                ),
                CatalogRecipeModelReference(
                    recipe_revision_id=RECIPE_REVISION_ID,
                    recipe_kind="recipe",
                    selection_id=recipe.models[0].id,
                    model_revision_id=MODEL_REVISION_ID,
                    model_kind="model",
                    model_publisher=model.identity.publisher,
                    model_slug=model.identity.slug,
                    model_content_digest=model_digest,
                ),
            ]
        )
        return session.get(CatalogDocumentRevision, RECIPE_REVISION_ID)


def setup(tmp_path: Path, *, options: bool = False):
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
                    "recipe.image.pull.v1",
                    "fabric.connected.mbps.200000",
                ),
                fabric_address=f"192.168.100.{index}",
                fabric_bandwidth_mbps=200_000,
                memory_pool="shared",
            )
        )
    revision = _seed_canonical_catalog(sessions, now, options=options)
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


def test_recipe_option_choices_make_a_distinct_mapping_and_default_when_unset(
    tmp_path: Path,
) -> None:
    from vonk_control.cluster_mappings import mapping_option_choices

    sessions, now, node_ids, revision = setup(tmp_path, options=True)
    service = ClusterMappingService(sessions)

    default = service.preview(revision.id, node_ids, {}, "admin")
    chosen = service.preview(
        revision.id,
        node_ids,
        {"option_choices": {"verification": "adaptive"}},
        "admin",
    )
    assert mapping_option_choices(default.parameters) == {"verification": "standard"}
    assert mapping_option_choices(chosen.parameters) == {"verification": "adaptive"}
    assert default.placement_digest != chosen.placement_digest
    first = service.materialize(default, actor="admin", now=now)
    second = service.materialize(chosen, actor="admin", now=now)
    assert first != second
    with pytest.raises(ClusterMappingError):
        service.preview(
            revision.id, node_ids, {"option_choices": {"verification": "x"}}, "admin"
        )


def test_mapping_preview_is_actor_bound_and_ready_nodes_are_immutable(
    tmp_path: Path,
) -> None:
    sessions, now, node_ids, revision = setup(tmp_path)
    service = ClusterMappingService(sessions)
    with pytest.raises(ClusterMappingError):
        service.preview(revision.id, node_ids, {}, " " * 201)
    mapping_id = service.materialize(
        service.preview(revision.id, node_ids, {}, "admin"), actor="admin", now=now
    )
    with (
        pytest.raises(ValueError),
        sessions.begin() as session,
    ):
        node = session.scalar(
            select(ClusterMappingNode).where(
                ClusterMappingNode.mapping_id == mapping_id
            )
        )
        assert node is not None
        node.role = "tampered"
    with sessions() as session:
        nodes = tuple(
            session.scalars(
                select(ClusterMappingNode).where(
                    ClusterMappingNode.mapping_id == mapping_id
                )
            )
        )
        assert sorted(node.rank for node in nodes) == [0, 1, 2]
        assert all(node.role != "tampered" for node in nodes)
    assert (
        service.materialize(
            service.preview(revision.id, node_ids, {}, "admin"), actor="admin", now=now
        )
        == mapping_id
    )


def test_mapping_rejects_wrong_node_count_and_missing_required_fabric(
    tmp_path: Path,
) -> None:
    sessions, _now, node_ids, revision = setup(tmp_path)
    service = ClusterMappingService(sessions)

    with pytest.raises(ClusterMappingError):
        service.preview(revision.id, node_ids[:2], {}, "admin")

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
            capabilities=(
                "runtime.vonk.v1",
                "recipe.image.pull.v1",
            ),
            memory_pool="shared",
        )
    )

    with pytest.raises(ClusterMappingError):
        service.preview(revision.id, node_ids, {}, "admin")


def test_mapping_rejects_forged_role_rank_and_endpoint_owner(tmp_path: Path) -> None:
    sessions, now, node_ids, revision = setup(tmp_path)
    service = ClusterMappingService(sessions)
    plan = service.preview(revision.id, node_ids, {}, "admin")
    forged_nodes = list(plan.nodes)
    forged_nodes[0] = replace(forged_nodes[0], role="worker", endpoint_owner=False)
    forged_nodes[1] = replace(forged_nodes[1], role="entrypoint", endpoint_owner=True)
    forged = replace(plan, nodes=tuple(forged_nodes))

    with pytest.raises(ClusterMappingError):
        service.materialize(forged, actor="admin", now=now)

    forged_nodes = list(plan.nodes)
    forged_nodes[0] = replace(forged_nodes[0], endpoint_owner=False)
    forged_nodes[1] = replace(forged_nodes[1], endpoint_owner=True)
    forged = replace(plan, nodes=tuple(forged_nodes))

    with pytest.raises(ClusterMappingError):
        service.materialize(forged, actor="admin", now=now)

    mapping_id = service.materialize(plan, actor="admin", now=now)
    with sessions() as session:
        assert session.get(ClusterMapping, mapping_id) is not None
