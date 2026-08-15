import json
import shutil
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from test_catalog_service import _seed_recipe_dependencies
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
)
from vonk_control.run_admission import RunAdmissionService, RunPlanConflict


def setup(
    tmp_path,
    *,
    free_memory=300,
    capabilities=("runtime.vonk.v1",),
    port_reserved=False,
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
            "system_reserve_bytes": 0,
        }
    )
    catalog = CatalogService(
        sessions, clock=lambda: now, cursors=TokenCodec(b"c" * 32).cursor_codec()
    )
    _seed_recipe_dependencies(catalog, document)
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


@pytest.fixture(scope="module")
def postgres_engine():
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for PostgreSQL run admission tests")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    name = f"vonk-run-admission-{uuid.uuid4().hex[:12]}"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "--name",
                name,
                "-e",
                "POSTGRES_PASSWORD=postgres",
                "-p",
                f"127.0.0.1:{port}:5432",
                "postgres:18.0-bookworm",
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        pytest.skip(f"disposable PostgreSQL is unavailable: {error}")
    engine = create_engine(
        f"postgresql+psycopg://postgres:postgres@127.0.0.1:{port}/postgres"
    )
    try:
        for _ in range(50):
            try:
                with engine.connect():
                    break
            except OperationalError:
                time.sleep(0.1)
        else:
            pytest.skip("disposable PostgreSQL did not become ready")
        yield engine
    finally:
        engine.dispose()
        subprocess.run(["docker", "stop", name], check=False, capture_output=True)


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
    )
    assert plan.alias == "qwen"
    assert alternate.alias == "qwen-alt"
    assert alternate.plan_digest != plan.plan_digest

    run_id = service.accept_run(plan, actor="admin", now=now)

    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        assert run.alias == plan.alias == run.plan["alias"]


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
