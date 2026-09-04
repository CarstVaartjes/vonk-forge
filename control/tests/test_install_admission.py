import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from vonk_control.artifact_sizes import ArtifactSize, StaticArtifactSizeResolver
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService, RecipeDraftInput
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.install_admission import InstallAdmissionService, InstallPlanConflict
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentNode,
    Base,
    ClusterMappingNode,
    NodeArtifact,
    RecipeBuild,
    RecipeInstallation,
    ResourceReservation,
)

from .test_catalog_service import _seed_recipe_dependencies

MODEL_SOURCE = "vonk-forge/synthetic-tiny@0123456789abcdef0123456789abcdef01234567"


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
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    document["identity"]["slug"] = "qwen3-vllm"
    disk = document["topology"]["roles"][0]["resources"]["disk"]
    disk.update(
        {
            "image_bytes": 30,
            "artifact_bytes": 70,
            "staging_bytes": 20,
            "cache_bytes": 0,
            "rollback_bytes": 0,
            "safety_margin_bytes": 10,
        }
    )
    catalog = CatalogService(
        sessions, clock=lambda: now, cursors=TokenCodec(b"c" * 32).cursor_codec()
    )
    _seed_recipe_dependencies(
        catalog,
        document,
        denied_jurisdictions=tuple(denied_jurisdictions),
    )
    draft = catalog.create_recipe("admin", RecipeDraftInput("qwen3-vllm", document))
    resolved = catalog.resolve(draft.recipe_id, 1, "admin")
    mappings = ClusterMappingService(sessions)
    mapping_plan = mappings.preview(resolved.id, (node_id,), {}, "admin")
    mapping_id = mappings.materialize(mapping_plan, actor="admin", now=now)
    with sessions.begin() as session:
        build = RecipeBuild(
            recipe_revision_id=resolved.id,
            builder_node_id=node_id,
            source_bundle_sha256=document["build"]["context"]["sha256"],
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
    service = InstallAdmissionService(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    )
    plan = service.plan_install(mapping, build, now=now)
    assert plan.allowed is True
    assert plan.nodes[0].required_bytes == 90
    assert plan.nodes[0].free_after_bytes == 10

    service = InstallAdmissionService(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=11
    )
    blocked = service.plan_install(mapping, build, now=now)
    assert blocked.allowed is False
    assert blocked.nodes[0].blockers[0].code == "install.insufficient_disk"


def test_territorial_license_install_admission_is_fail_closed(tmp_path) -> None:
    sessions, now, _node, mapping, build, sizes = setup(
        tmp_path,
        denied_jurisdictions=("EU", "GB", "KR"),
    )

    unconfigured = InstallAdmissionService(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    ).plan_install(mapping, build, now=now)
    assert unconfigured.allowed is False
    assert unconfigured.nodes[0].blockers[0].code == (
        "install.license_jurisdiction_required"
    )
    assert "VONK_OPERATOR_JURISDICTION" in unconfigured.nodes[0].blockers[0].detail

    eu_member = InstallAdmissionService(
        sessions,
        sizes=sizes,
        inventory_max_age=300,
        disk_floor_bytes=10,
        operator_jurisdiction="NL",
    ).plan_install(mapping, build, now=now)
    assert eu_member.allowed is False
    assert eu_member.nodes[0].blockers[0].code == ("install.license_territory_denied")
    assert "NL" in eu_member.nodes[0].blockers[0].detail

    permitted = InstallAdmissionService(
        sessions,
        sizes=sizes,
        inventory_max_age=300,
        disk_floor_bytes=10,
        operator_jurisdiction="US",
    ).plan_install(mapping, build, now=now)
    assert permitted.allowed is True
    assert permitted.nodes[0].warnings[0].code == ("install.license_territory_checked")


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
    plan = InstallAdmissionService(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    ).plan_install(mapping, build, now=now)
    assert plan.allowed is True
    assert plan.nodes[0].reused_bytes == 100
    assert plan.nodes[0].required_bytes == 20


def test_accepted_plan_persists_mapping_build_and_disk_reservation(tmp_path) -> None:
    sessions, now, _node, mapping, build, sizes = setup(tmp_path, free=200)
    service = InstallAdmissionService(
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
    service = InstallAdmissionService(
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
    stale = InstallAdmissionService(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    ).plan_install(mapping, build, now=now)
    assert any(
        item.code == "install.stale_inventory" for item in stale.nodes[0].blockers
    )

    sessions, now, _node, mapping, build, sizes = setup(
        tmp_path / "read-only", free=200, read_only=True
    )
    blocked = InstallAdmissionService(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    ).plan_install(mapping, build, now=now)
    assert any(
        item.code == "install.artifact_store_read_only"
        for item in blocked.nodes[0].blockers
    )


def test_install_topology_uses_authenticated_inventory_capabilities(tmp_path) -> None:
    sessions, now, node, mapping, build, sizes = setup(tmp_path, free=200)
    with sessions.begin() as session:
        registered = session.get(AgentNode, node)
        assert registered is not None
        registered.capabilities = []

    plan = InstallAdmissionService(
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

    plan = InstallAdmissionService(
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
