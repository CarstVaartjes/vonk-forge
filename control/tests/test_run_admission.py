import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentNode,
    Base,
    InstallationNode,
    LocalRecipe,
    LocalRecipeRevision,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.run_admission import RunAdmissionService, RunPlanConflict

from .test_catalog_service import _seed_recipe_dependencies


def setup(
    tmp_path,
    *,
    free_memory=300,
    capabilities=("runtime.vonk.v1",),
    port_reserved=False,
    system_reserve=0,
    denied_jurisdictions=(),
    engine=None,
):
    engine = engine or create_engine(f"sqlite:///{tmp_path / 'run.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    node = "spk_" + "1" * 32
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    memory = document["topology"]["roles"][0]["resources"]["memory"]
    memory.update(
        {
            "startup_peak_bytes": 225,
            "steady_state_bytes": 200,
            "runtime_growth_bytes": 25,
            "system_reserve_bytes": system_reserve,
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
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node,
                state="active",
                architecture="linux-arm64",
                capabilities=["runtime.vonk.v1"],
            )
        )
        recipe = LocalRecipe(
            slug="qwen",
            title="Qwen",
            description="Qwen",
            source_kind="local",
            created_by="admin",
            created_at=now,
            updated_at=now,
        )
        session.add(recipe)
        session.flush()
        revision = LocalRecipeRevision(
            recipe_id=recipe.id,
            revision_number=1,
            lifecycle="resolved",
            schema_version=1,
            document=document,
            content_sha256="a" * 64,
            created_by="admin",
            created_at=now,
        )
        session.add(revision)
        session.flush()
        revision_id = revision.id
    mappings = ClusterMappingService(sessions)
    mapping_plan = mappings.preview(revision_id, (node,), {}, "admin")
    mapping_id = mappings.materialize(mapping_plan, actor="admin", now=now)
    with sessions.begin() as session:
        build = RecipeBuild(
            recipe_revision_id=revision_id,
            builder_node_id=node,
            source_bundle_sha256=document["build"]["context"]["sha256"],
            build_input_sha256="e" * 64,
            state="succeeded",
            policy_report={"passed": True},
            plan={},
            image_digest="sha256:" + "f" * 64,
            oci_layout_sha256="0" * 64,
            image_bytes=1,
            created_at=now,
            updated_at=now,
        )
        session.add(build)
        session.flush()
        installation = RecipeInstallation(
            recipe_revision_id=revision_id,
            mapping_id=mapping_id,
            mapping_generation=1,
            recipe_build_id=build.id,
            image_digest=build.image_digest,
            plan_digest="b" * 64,
            plan={},
            state="installed",
            actor="admin",
            created_at=now,
            updated_at=now,
        )
        session.add(installation)
        session.flush()
        session.add(
            InstallationNode(
                installation_id=installation.id,
                node_id=node,
                rank=0,
                role="entrypoint",
                state="installed",
                required_bytes=1,
                installed_bytes=1,
                updated_at=now,
            )
        )
        if port_reserved:
            session.add(
                ResourceReservation(
                    node_id=node,
                    kind="port",
                    resource_key="8000",
                    amount_bytes=0,
                    owner_kind="run",
                    owner_id="1" * 36,
                    state="active",
                    plan_digest="c" * 64,
                    created_at=now,
                )
            )
    InventoryRepository(sessions, clock=lambda: now).record(
        InventorySnapshotInput(
            node,
            now,
            1000,
            500,
            1000,
            free_memory,
            1000,
            free_memory,
            1,
            False,
            tuple(capabilities),
        )
    )
    return sessions, now, node, installation.id


def test_run_alias_is_digest_bound_and_persisted_with_plan_authority(tmp_path) -> None:
    sessions, now, _node, installation = setup(tmp_path, free_memory=300)
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    )
    plan = service.plan_run(installation, alias="qwen", now=now)
    alternate = service.plan_run(installation, alias="qwen-alt", now=now)
    assert (
        plan.allowed is True
        and plan.nodes[0].required_memory_bytes == 225
        and plan.nodes[0].free_after_bytes == 75
        and plan.nodes[0].memory_floor_bytes == 50
    )
    assert plan.alias == "qwen"
    assert alternate.alias == "qwen-alt"
    assert alternate.plan_digest != plan.plan_digest

    run_id = service.accept_run(plan, actor="admin", now=now)

    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        assert run.alias == plan.alias == run.plan["alias"]


def test_territorial_license_run_admission_is_fail_closed(tmp_path) -> None:
    sessions, now, _node, installation = setup(
        tmp_path,
        free_memory=300,
        denied_jurisdictions=("EU", "GB", "KR"),
    )

    unconfigured = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    ).plan_run(installation, alias="hunyuan", now=now)
    assert unconfigured.allowed is False
    assert unconfigured.nodes[0].blockers[0].code == (
        "run.license_jurisdiction_required"
    )

    south_korea = RunAdmissionService(
        sessions,
        inventory_max_age=300,
        memory_floor_bytes=50,
        operator_jurisdiction="KR",
    ).plan_run(installation, alias="hunyuan", now=now)
    assert south_korea.allowed is False
    assert south_korea.nodes[0].blockers[0].code == ("run.license_territory_denied")

    permitted = RunAdmissionService(
        sessions,
        inventory_max_age=300,
        memory_floor_bytes=50,
        operator_jurisdiction="US",
    ).plan_run(installation, alias="hunyuan", now=now)
    assert permitted.allowed is True
    assert permitted.nodes[0].warnings[0].code == "run.license_territory_checked"


def test_system_reserve_is_a_floor_not_workload_memory(tmp_path) -> None:
    sessions, now, _node, installation = setup(
        tmp_path,
        free_memory=300,
        system_reserve=75,
    )
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    )

    plan = service.plan_run(installation, alias="qwen", now=now)

    assert plan.allowed is True
    assert plan.nodes[0].required_memory_bytes == 225
    assert plan.nodes[0].free_after_bytes == 75
    assert plan.nodes[0].memory_floor_bytes == 75


def test_system_reserve_still_blocks_a_run_without_headroom(tmp_path) -> None:
    sessions, now, _node, installation = setup(
        tmp_path,
        free_memory=299,
        system_reserve=75,
    )
    plan = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    ).plan_run(installation, alias="qwen", now=now)

    assert plan.allowed is False
    assert plan.nodes[0].required_memory_bytes == 225
    assert plan.nodes[0].free_after_bytes == 74
    assert plan.nodes[0].memory_floor_bytes == 75
    assert "run.insufficient_memory" in {
        reason.code for reason in plan.nodes[0].blockers
    }


def test_stopped_run_can_repeat_the_same_plan_digest(tmp_path) -> None:
    sessions, now, _node, installation = setup(tmp_path, free_memory=300)
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    )
    plan = service.plan_run(installation, alias="qwen", now=now)
    first_run_id = service.accept_run(plan, actor="admin", now=now)
    with sessions.begin() as session:
        first_run = session.get(RecipeRun, first_run_id)
        assert first_run is not None
        first_run.state = "stopped"
        first_run.stopped_at = now
        for node in session.scalars(
            select(RunNode).where(RunNode.run_id == first_run_id)
        ):
            node.state = "stopped"
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == first_run_id,
            )
        ):
            reservation.state = "released"
            reservation.released_at = now

    repeated_plan = service.plan_run(installation, alias="qwen", now=now)
    assert repeated_plan.plan_digest == plan.plan_digest

    second_run_id = service.accept_run(repeated_plan, actor="admin", now=now)

    assert second_run_id != first_run_id
    with sessions() as session:
        repeated_runs = tuple(
            session.scalars(
                select(RecipeRun)
                .where(RecipeRun.plan_digest == plan.plan_digest)
                .order_by(RecipeRun.id)
            )
        )
        assert {run.id for run in repeated_runs} == {first_run_id, second_run_id}
        assert {run.state for run in repeated_runs} == {"planned", "stopped"}


def test_memory_capability_and_port_conflicts_are_explained(tmp_path) -> None:
    sessions, now, _node, installation = setup(
        tmp_path,
        free_memory=260,
        capabilities=("runtime.sglang.v1",),
        port_reserved=True,
    )
    plan = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    ).plan_run(installation, "qwen", now=now)
    codes = {reason.code for reason in plan.nodes[0].blockers}
    assert {
        "run.insufficient_memory",
        "topology.runtime_capability_missing",
        "run.port_occupied",
    } <= codes


def test_accept_rechecks_memory_reservations_while_holding_node_lock(tmp_path) -> None:
    sessions, now, node, installation = setup(tmp_path, free_memory=300)
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    )
    plan = service.plan_run(installation, "qwen", now=now)
    with sessions.begin() as session:
        session.add_all(
            [
                ResourceReservation(
                    node_id=node,
                    kind=kind,
                    resource_key="concurrent",
                    amount_bytes=50,
                    owner_kind="run",
                    owner_id="2" * 36,
                    state="active",
                    plan_digest="d" * 64,
                    created_at=now,
                )
                for kind in ("unified-memory",)
            ]
        )
    service.plan_run = lambda *args, **kwargs: plan

    with pytest.raises(RunPlanConflict, match="memory capacity changed"):
        service.accept_run(plan, actor="admin", now=now)


def test_queue_rejects_reservation_mutation_after_preview(tmp_path) -> None:
    sessions, now, node, installation = setup(tmp_path, free_memory=300)
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    )
    plan = service.plan_run(installation, "qwen", now=now)
    with sessions.begin() as session:
        session.add(
            ResourceReservation(
                node_id=node,
                kind="unified-memory",
                resource_key="between-preview-and-queue",
                amount_bytes=50,
                owner_kind="run",
                owner_id="2" * 36,
                state="active",
                plan_digest="d" * 64,
                created_at=now,
            )
        )
    with pytest.raises(RunPlanConflict, match="run.plan_stale_or_blocked"):
        service.accept_run(plan, actor="admin", now=now)


def test_postgres_competing_admissions_have_one_capacity_winner(
    tmp_path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, now, _node, installation = setup(
        tmp_path, free_memory=300, engine=postgres_engine
    )
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    )
    plans = {
        alias: service.plan_run(installation, alias, now=now)
        for alias in ("qwen-a", "qwen-b")
    }
    barrier = threading.Barrier(2)

    def accept(alias: str) -> bool:
        barrier.wait()
        try:
            service.accept_run(plans[alias], actor="admin", now=now)
        except RunPlanConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(accept, ("qwen-a", "qwen-b")))

    assert sorted(outcomes) == [False, True]
    with sessions() as session:
        active = session.query(ResourceReservation).filter_by(state="active").all()
        assert len(active) == 2
        assert len({row.owner_id for row in active}) == 1
