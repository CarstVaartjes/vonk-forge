import json
from datetime import UTC, datetime, timedelta
from importlib.resources import files

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from vonk_control.artifact_sizes import ArtifactSize, StaticArtifactSizeResolver
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.install_admission import InstallAdmissionService, InstallPlanConflict
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
    ClusterMappingNode,
    NodeArtifact,
    RecipeBuild,
    RecipeInstallation,
    ResourceReservation,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

MODEL_SOURCE = "vonk-forge/synthetic-tiny@0123456789abcdef0123456789abcdef01234567"
MODEL_DOCUMENT_ID = "00000000-0000-4000-8000-000000000010"
MODEL_REVISION_ID = "00000000-0000-4000-8000-000000000011"
RECIPE_DOCUMENT_ID = "00000000-0000-4000-8000-000000000020"
RECIPE_REVISION_ID = "00000000-0000-4000-8000-000000000021"


def _canonical_catalog_documents(
    *, denied_jurisdictions: tuple[str, ...] = ()
) -> tuple[ModelDefinition, RecipeDefinition]:
    raw_model = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    if denied_jurisdictions:
        raw_model["license"]["territorial_restrictions"] = {
            "denied_jurisdictions": list(denied_jurisdictions),
            "notice": "Synthetic test restrictions.",
        }
    model = ModelDefinition.model_validate(raw_model)
    raw_recipe = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-image.json")
        .read_text(encoding="utf-8")
    )
    raw_recipe["identity"]["slug"] = "qwen3-vllm"
    raw_recipe["settings"]["knobs"]["max_model_len"] = {
        "value": 32768,
        "change_effect": "restart",
    }
    raw_recipe["topology"]["roles"][0]["resources"]["disk"].update(
        {
            "image_bytes": 30,
            "artifact_bytes": 70,
            "staging_bytes": 20,
            "cache_bytes": 0,
            "rollback_bytes": 0,
            "safety_margin_bytes": 10,
        }
    )
    raw_recipe["models"][0]["model"]["content_sha256"] = content_sha256(model)
    return model, RecipeDefinition.model_validate(raw_recipe)


def _seed_canonical_catalog(
    sessions: sessionmaker,
    now: datetime,
    *,
    denied_jurisdictions: tuple[str, ...] = (),
) -> CatalogDocumentRevision:
    model, recipe = _canonical_catalog_documents(
        denied_jurisdictions=denied_jurisdictions
    )
    model_digest = content_sha256(model)
    recipe_digest = content_sha256(recipe)
    model_document = model.model_dump(mode="json")
    stored_model = ModelDefinition.model_validate(model_document)
    assert content_sha256(stored_model) == model_digest
    recipe_document = recipe.model_dump(mode="json")
    stored_recipe = RecipeDefinition.model_validate(recipe_document)
    assert stored_recipe.models[0].model.content_sha256 == model_digest
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
                    document=model_document,
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
                    document=recipe_document,
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
        session.flush()
        stored_model_revision = session.get(
            CatalogDocumentRevision, MODEL_REVISION_ID
        )
        assert stored_model_revision is not None
        assert content_sha256(
            ModelDefinition.model_validate(stored_model_revision.document)
        ) == stored_model_revision.content_digest
        stored_recipe_revision = session.get(
            CatalogDocumentRevision, RECIPE_REVISION_ID
        )
        assert stored_recipe_revision is not None
        assert (
            RecipeDefinition.model_validate(stored_recipe_revision.document)
            .models[0]
            .model.content_sha256
            == stored_model_revision.content_digest
        )
        reference = session.scalar(
            select(CatalogRecipeModelReference).where(
                CatalogRecipeModelReference.recipe_revision_id == RECIPE_REVISION_ID,
                CatalogRecipeModelReference.selection_id == recipe.models[0].id,
            )
        )
        assert reference is not None
        assert (
            reference.model_kind,
            reference.model_publisher,
            reference.model_slug,
            reference.model_content_digest,
        ) == (
            "model",
            model.identity.publisher,
            model.identity.slug,
            model_digest,
        )
        revision = session.get(CatalogDocumentRevision, RECIPE_REVISION_ID)
        assert revision is not None
        return revision


def _compiled_plan(
    *, role: str, rank: int, model_digest: str, recipe_digest: str, build_input: str
) -> dict[str, object]:
    artifact_digest = "3" * 64
    image_digest = "sha256:" + "1" * 64
    layout_digest = "2" * 64
    return {
        "schema_version": 2,
        "identity": {
            "recipe_revision_sha256": recipe_digest,
            "execution_sha256": "b" * 64,
            "harness_sha256": "c" * 64,
            "build_input_sha256": build_input,
            "model_artifact_set_sha256": "e" * 64,
            "model_artifact_bytes": 70,
        },
        "runtime": {
            "executable": "/opt/vonk/bin/vllm",
            "argv": ["serve"],
            "env": [],
            "image_digest": image_digest,
            "placement": {
                "endpoint_address": None,
                "rank": rank,
                "role": role,
                "world_size": 1,
                "local_address": None,
                "master_address": None,
                "master_port": None,
                "port": 8000,
                "reserved_memory_bytes": 1,
            },
        },
        "artifacts": [
            {
                "selection_id": "primary",
                "file_id": "weights",
                "path": "model.safetensors",
                "sha256": artifact_digest,
                "size_bytes": 70,
                "roles": ["entrypoint", "weights"],
                "mount": {"target": "/models", "read_only": True},
                "model": {
                    "publisher": "vonk-forge",
                    "slug": "synthetic-tiny-fp16",
                    "content_sha256": model_digest,
                },
                "distribution_object": {
                    "name": "model.safetensors",
                    "sha256": artifact_digest,
                    "bytes": 70,
                    "kind": "model",
                },
            }
        ],
        "runtime_image": {
            "image_digest": image_digest,
            "oci_layout_sha256": layout_digest,
            "image_bytes": 30,
            "architecture": "linux-arm64",
            "runtime_interface": "vonk.runtime.v1",
            "source": "controller-build",
            "build_id": "test-build",
            "registry_manifest_digest": None,
            "platform_manifest_digest": image_digest,
            "local_image_config_id": "sha256:" + "4" * 64,
            "runtime_interface_label": "v1",
            "distribution_object": {
                "name": "image.oci.tar",
                "sha256": layout_digest,
                "bytes": 30,
                "kind": "oci-archive",
            },
        },
        "security": {
            "devices": [],
            "capabilities": [],
            "network_mode": "none",
            "host_network": False,
            "privileged": False,
            "user": "10001:10001",
            "mounts": [{"source": "model", "target": "/models", "read_only": True}],
            "read_only_root": True,
            "no_new_privileges": True,
        },
        "topology": {
            "name": "solo",
            "mode": "single",
            "backend": "local",
            "node_count": 1,
            "world_size": 1,
            "rank": rank,
            "role": role,
        },
        "lifecycle": {
            "pre_start": [],
            "post_stop": [],
            "stop_timeout_seconds": 30,
        },
        "endpoint": {
            "protocol": "openai",
            "port": 8000,
            "model_aliases": ["synthetic-tiny"],
            "health_path": "/v1/models",
        },
        "job": None,
    }


def _compiled_plan_provider(**kwargs: object) -> dict[str, dict[str, object]]:
    mapping_nodes = kwargs["mapping_nodes"]
    revision = kwargs["revision"]
    build = kwargs["build"]
    resolved_entities = kwargs["resolved_entities"]
    model_revision = resolved_entities["models"][0]
    return {
        node.node_id: _compiled_plan(
            role=node.role,
            rank=node.rank,
            model_digest=model_revision.content_digest,
            recipe_digest=revision.content_digest,
            build_input=build.build_input_sha256,
        )
        for node in mapping_nodes
    }


def _service(sessions, sizes, **kwargs):
    return InstallAdmissionService(
        sessions,
        sizes=sizes,
        compiled_plan_provider=_compiled_plan_provider,
        **kwargs,
    )


def setup(
    tmp_path,
    *,
    free=200,
    read_only=False,
    observed_age=0,
    denied_jurisdictions=(),
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'install.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    node_id = "spk_" + "1" * 32
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                architecture="linux-arm64",
                capabilities=["runtime.vonk.v1"],
            )
        )
    InventoryRepository(sessions, clock=lambda: now).record(
        InventorySnapshotInput(
            node_id,
            now - timedelta(seconds=observed_age),
            1000,
            free,
            1000,
            800,
            1000,
            800,
            1,
            read_only,
            ("runtime.vonk.v1",),
        )
    )
    resolved = _seed_canonical_catalog(
        sessions, now, denied_jurisdictions=tuple(denied_jurisdictions)
    )
    mappings = ClusterMappingService(sessions)
    mapping_plan = mappings.preview(resolved.id, (node_id,), {}, "admin")
    mapping_id = mappings.materialize(mapping_plan, actor="admin", now=now)
    with sessions.begin() as session:
        build = RecipeBuild(
            recipe_revision_id=resolved.id,
            builder_node_id=node_id,
            source_bundle_sha256="c" * 64,
            build_input_sha256="b" * 64,
            state="succeeded",
            policy_report={"passed": True},
            plan={},
            image_digest="sha256:" + "1" * 64,
            oci_layout_sha256="2" * 64,
            image_bytes=30,
            created_at=now,
            updated_at=now,
        )
        session.add(build)
        session.flush()
        build_id = build.id
        session.add(
            NodeArtifact(
                node_id=node_id,
                kind="image",
                digest="1" * 64,
                source="docker-archive:" + "2" * 64,
                size_bytes=30,
                state="verified",
                ref_count=0,
                verified_at=now,
                updated_at=now,
            )
        )
    sizes = StaticArtifactSizeResolver((ArtifactSize(MODEL_SOURCE, "3" * 64, 70),))
    return sessions, now, node_id, mapping_id, build_id, sizes


def test_exact_fit_and_safety_floor_are_explained(tmp_path) -> None:
    sessions, now, _node, mapping, build, sizes = setup(tmp_path, free=100)
    service = _service(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    )
    plan = service.plan_install(mapping, build, now=now)
    assert plan.allowed is True
    assert plan.nodes[0].required_bytes == 90
    assert plan.nodes[0].free_after_bytes == 10

    service = _service(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=11
    )
    blocked = service.plan_install(mapping, build, now=now)
    assert blocked.allowed is False
    assert blocked.nodes[0].blockers[0].code == "install.insufficient_disk"


def test_territorial_license_install_admission_is_informational(tmp_path) -> None:
    sessions, now, _node, mapping, build, sizes = setup(
        tmp_path,
        denied_jurisdictions=("EU", "GB", "KR"),
    )

    unconfigured = _service(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    ).plan_install(mapping, build, now=now)
    assert unconfigured.allowed is True
    assert not any(
        blocker.code.startswith("install.license.")
        for blocker in unconfigured.nodes[0].blockers
    )
    assert unconfigured.nodes[0].warnings[0].code == (
        "install.license_territorial_restrictions_informational"
    )

    eu_member = _service(
        sessions,
        sizes=sizes,
        inventory_max_age=300,
        disk_floor_bytes=10,
        operator_jurisdiction="NL",
    ).plan_install(mapping, build, now=now)
    assert eu_member.allowed is True
    assert eu_member.nodes[0].warnings[0].code == (
        "install.license_territorial_restrictions_informational"
    )

    permitted = _service(
        sessions,
        sizes=sizes,
        inventory_max_age=300,
        disk_floor_bytes=10,
        operator_jurisdiction="US",
    ).plan_install(mapping, build, now=now)
    assert permitted.allowed is True
    assert permitted.nodes[0].warnings[0].code == (
        "install.license_territorial_restrictions_informational"
    )


def test_verified_existing_artifacts_reduce_disk_and_download(tmp_path) -> None:
    sessions, now, node, mapping, build, sizes = setup(tmp_path, free=80)
    with sessions.begin() as session:
        session.add(
            NodeArtifact(
                node_id=node,
                kind="model",
                digest="3" * 64,
                source=MODEL_SOURCE,
                size_bytes=70,
                state="verified",
                ref_count=0,
                verified_at=now,
                updated_at=now,
            )
        )
    plan = _service(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    ).plan_install(mapping, build, now=now)
    assert plan.allowed is True
    assert plan.nodes[0].reused_bytes == 100
    assert plan.nodes[0].required_bytes == 20


def test_accepted_plan_persists_mapping_build_and_disk_reservation(tmp_path) -> None:
    sessions, now, _node, mapping, build, sizes = setup(tmp_path, free=200)
    service = _service(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    )
    plan = service.plan_install(mapping, build, now=now)
    installation_id = service.accept_install(plan, actor="admin", now=now)
    with sessions() as session:
        installation = session.get(RecipeInstallation, installation_id)
        reservation = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == installation_id
            )
        )
        assert installation.mapping_id == mapping
        assert installation.recipe_build_id == build
        assert installation.mapping_generation == 1
        assert reservation.amount_bytes == plan.nodes[0].required_bytes


def test_queue_rejects_artifact_or_reservation_mutation_after_preview(tmp_path) -> None:
    sessions, now, node, mapping, build, sizes = setup(tmp_path, free=200)
    service = _service(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    )
    plan = service.plan_install(mapping, build, now=now)
    with sessions.begin() as session:
        artifact = session.scalar(
            select(NodeArtifact).where(NodeArtifact.node_id == node)
        )
        assert artifact is not None
        artifact.state = "missing"
        session.add(
            ResourceReservation(
                node_id=node,
                kind="disk",
                resource_key="between-preview-and-queue",
                amount_bytes=200,
                owner_kind="installation",
                owner_id="1" * 36,
                state="active",
                plan_digest="a" * 64,
                created_at=now,
            )
        )
    with pytest.raises(InstallPlanConflict, match="install.plan_stale_or_blocked"):
        service.accept_install(plan, actor="admin", now=now)


def test_stale_and_read_only_inventory_are_blocking(tmp_path) -> None:
    sessions, now, _node, mapping, build, sizes = setup(
        tmp_path, free=200, observed_age=301
    )
    stale = _service(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    ).plan_install(mapping, build, now=now)
    assert any(
        item.code == "install.stale_inventory" for item in stale.nodes[0].blockers
    )

    sessions, now, _node, mapping, build, sizes = setup(
        tmp_path / "read-only", free=200, read_only=True
    )
    blocked = _service(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    ).plan_install(mapping, build, now=now)
    assert any(
        item.code == "install.artifact_store_read_only"
        for item in blocked.nodes[0].blockers
    )


def test_plan_digest_ignores_fresh_inventory_observation_noise(tmp_path) -> None:
    sessions, now, node, mapping, build, sizes = setup(tmp_path, free=200)
    service = _service(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    )
    original = service.plan_install(mapping, build, now=now)
    InventoryRepository(sessions, clock=lambda: now).record(
        InventorySnapshotInput(
            node,
            now + timedelta(seconds=1),
            1000,
            199,
            1000,
            800,
            1000,
            800,
            1,
            False,
            ("runtime.vonk.v1",),
        )
    )

    refreshed = service.plan_install(
        mapping, build, now=now + timedelta(seconds=1)
    )

    assert refreshed.allowed is True
    assert refreshed.nodes[0].inventory_observed_at != (
        original.nodes[0].inventory_observed_at
    )
    assert refreshed.nodes[0].free_bytes != original.nodes[0].free_bytes
    assert refreshed.plan_digest == original.plan_digest


def test_apply_revalidates_but_tolerates_nonblocking_reservation_noise(
    tmp_path,
) -> None:
    sessions, now, node, mapping, build, sizes = setup(tmp_path, free=200)
    service = _service(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    )
    plan = service.plan_install(mapping, build, now=now)
    with sessions.begin() as session:
        session.add(
            ResourceReservation(
                node_id=node,
                kind="disk",
                resource_key="harmless-concurrent-reservation",
                amount_bytes=1,
                owner_kind="installation",
                owner_id="2" * 36,
                state="active",
                plan_digest="a" * 64,
                created_at=now,
            )
        )

    installation_id = service.accept_install(plan, actor="admin", now=now)

    assert installation_id


def test_install_topology_uses_authenticated_inventory_capabilities(tmp_path) -> None:
    sessions, now, node, mapping, build, sizes = setup(tmp_path, free=200)
    with sessions.begin() as session:
        registered = session.get(AgentNode, node)
        assert registered is not None
        registered.capabilities = []

    plan = _service(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    ).plan_install(mapping, build, now=now)

    assert plan.allowed is True


def test_install_topology_capability_loss_is_a_plan_blocker(tmp_path) -> None:
    sessions, now, node, mapping, build, sizes = setup(tmp_path, free=200)
    with sessions.begin() as session:
        registered = session.get(AgentNode, node)
        assert registered is not None
        registered.capabilities = []
    InventoryRepository(sessions, clock=lambda: now).record(
        InventorySnapshotInput(
            node,
            now + timedelta(seconds=1),
            1000,
            200,
            1000,
            800,
            1000,
            800,
            1,
            False,
            (),
        )
    )

    plan = _service(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    ).plan_install(mapping, build, now=now + timedelta(seconds=1))

    assert plan.allowed is False
    assert plan.nodes[0].blockers[0].code == "topology.runtime_capability_missing"


def test_database_rejects_mutable_built_image_identity(tmp_path) -> None:
    sessions, _now, _node, _mapping, build, _sizes = setup(tmp_path, free=200)
    with pytest.raises(IntegrityError), sessions.begin() as session:
        row = session.get(RecipeBuild, build)
        assert row is not None
        row.image_digest = "latest"


def test_install_rejects_mapping_with_wrong_endpoint_owner(tmp_path) -> None:
    sessions, _now, _node, mapping, _build, _sizes = setup(tmp_path, free=200)
    with (
        pytest.raises(ValueError, match="mapping.ready_immutable"),
        sessions.begin() as session,
    ):
        node = session.scalar(
            select(ClusterMappingNode).where(ClusterMappingNode.mapping_id == mapping)
        )
        assert node is not None
        node.endpoint_owner = False
