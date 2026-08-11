from __future__ import annotations

import hashlib
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
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.artifact_sizes import ArtifactSize, StaticArtifactSizeResolver
from vonk_control.catalog_service import CatalogService, RecipeDraftInput
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentPresence,
    Base,
    InstallationNode,
    Job,
    NodeArtifact,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
    RecipeRunObservation,
    record_recipe_run_observations,
)
from vonk_control.run_admission import RunAdmissionService


class RecordingQueue:
    def __init__(self) -> None:
        self.available = 0

    def enqueue_in_session(
        self,
        session,
        parent_job_id,
        node_id,
        operation,
        base_commit,
        payload,
        *,
        operation_id,
    ):
        row = AgentOperation(
            id=operation_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            kind=operation,
            payload_digest="f" * 64,
            payload=dict(payload),
            base_commit=base_commit,
            state="queued",
            current_attempt=0,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(row)
        return row

    def notify_available(self) -> None:
        self.available += 1


class FailingQueue(RecordingQueue):
    def enqueue_in_session(self, *args, **kwargs):
        raise RuntimeError("queue write failed")


NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def start_evidence(payload: dict[str, object]) -> dict[str, object]:
    identity = {
        "recipe_revision_id": payload["recipe_revision_id"],
        "recipe_content_sha256": payload["recipe_content_sha256"],
        "image_digest": str(payload["image_digest"]).removeprefix("sha256:"),
        "artifact_set_digest": "b" * 64,
        "model_identity": "Qwen/Qwen3-30B-A3B-Instruct-2507@0123456789abcdef0123456789abcdef01234567",
        "rank": payload["rank"],
        "world_size": payload["world_size"],
        "endpoint": f"http://{payload['endpoint_address']}:{payload['port']}",
        "memory_reservation_bytes": payload["reserved_memory_bytes"],
        "ready": True,
    }
    return {
        **identity,
        "evidence_digest": hashlib.sha256(canonical_message(identity)).hexdigest(),
    }


def setup_services(tmp_path: Path, *, nodes: int = 1, engine=None):
    engine = engine or create_engine(
        f"sqlite:///{tmp_path / 'operations.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    node_ids = tuple("spk_" + f"{index + 1:032x}" for index in range(nodes))
    with sessions.begin() as session:
        for index, node_id in enumerate(node_ids):
            serial = f"serial-{index}"
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    architecture="linux-arm64",
                    capabilities=["runtime.vonk.v1", "recipe.operations.v1"],
                )
            )
            session.flush()
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    fingerprint=f"fingerprint-{index}",
                    not_before=NOW,
                    not_after=datetime(2027, 8, 7, 12, tzinfo=UTC),
                )
            )
            session.add(
                AgentPresence(
                    node_id=node_id,
                    certificate_serial=serial,
                    certificate_fingerprint=f"fingerprint-{index}",
                    management_address=f"192.168.1.{211 + index}",
                    observed_at=NOW,
                )
            )
    inventory = InventoryRepository(sessions, clock=lambda: NOW)
    capabilities = ("runtime.vonk.v1", "recipe.operations.v1") + (
        ("fabric.connected.mbps.1000",) if nodes > 1 else ()
    )
    for index, node_id in enumerate(node_ids):
        inventory.record(
            InventorySnapshotInput(
                node_id,
                NOW,
                10_000,
                8_000,
                10_000,
                8_000,
                10_000,
                8_000,
                1,
                False,
                capabilities,
                fabric_address=(f"192.168.100.{index + 2}" if nodes > 1 else None),
                fabric_bandwidth_mbps=(1000 if nodes > 1 else None),
            )
        )
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    role = document["deployment_profiles"][0]["roles"][0]
    role["resources"] = {
        "disk": {
            "image_bytes": 30,
            "artifact_bytes": 70,
            "staging_bytes": 20,
            "cache_bytes": 0,
            "rollback_bytes": 0,
            "safety_margin_bytes": 10,
        },
        "memory": {
            "kind": "unified",
            "startup_peak_bytes": 225,
            "steady_state_bytes": 200,
            "runtime_growth_bytes": 25,
            "system_reserve_bytes": 0,
        },
    }
    if nodes > 1:
        worker = json.loads(json.dumps(role))
        worker.update({"name": "worker", "count": nodes - 1, "endpoint_owner": False})
        document["deployment_profiles"] = [
            {
                **document["deployment_profiles"][0],
                "name": f"nodes_{nodes}",
                "node_count": nodes,
                "strategy": "tensor_parallel",
                "parallelism": {
                    "tensor": nodes,
                    "pipeline": 1,
                    "data": 1,
                    "backend": "tcp",
                },
                "roles": [role, worker],
                "fabric": {"connectivity": "connected", "minimum_bandwidth_mbps": 1},
            }
        ]
        document["artifacts"][0]["roles"] = ["entrypoint", "worker"]
    catalog = CatalogService(sessions, clock=lambda: NOW)
    draft = catalog.create_recipe("admin", RecipeDraftInput("qwen3-vllm", document))
    revision = catalog.resolve(draft.recipe_id, 1, "admin")
    profile_name = "solo" if nodes == 1 else f"nodes_{nodes}"
    mappings = ClusterMappingService(sessions)
    mapping_plan = mappings.plan(revision.id, profile_name, node_ids, parameters={})
    mapping_id = mappings.materialize(mapping_plan, actor="admin", now=NOW)
    with sessions.begin() as session:
        build = RecipeBuild(
            recipe_revision_id=revision.id,
            builder_node_id=node_ids[0],
            source_bundle_sha256=document["build"]["context"]["sha256"],
            build_input_sha256="e" * 64,
            state="succeeded",
            policy_report={"passed": True},
            plan={},
            image_digest="sha256:" + "1" * 64,
            oci_layout_sha256="3" * 64,
            image_bytes=30,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(build)
        session.flush()
        build_id = build.id
        session.add_all(
            NodeArtifact(
                node_id=node_id,
                kind="image",
                digest="1" * 64,
                source="oci-layout:" + "3" * 64,
                size_bytes=30,
                state="verified",
                ref_count=0,
                verified_at=NOW,
                updated_at=NOW,
            )
            for node_id in node_ids
        )
    sizes = StaticArtifactSizeResolver(
        (
            ArtifactSize(
                "Qwen/Qwen3-30B-A3B-Instruct-2507@"
                "0123456789abcdef0123456789abcdef01234567",
                "2" * 64,
                70,
            ),
        )
    )
    install = InstallAdmissionService(
        sessions, sizes=sizes, inventory_max_age=300, disk_floor_bytes=10
    )
    run = RunAdmissionService(sessions, inventory_max_age=300, memory_floor_bytes=50)
    queue = RecordingQueue()
    service = RecipeOperationService(
        sessions,
        install_admission=install,
        run_admission=run,
        agent_jobs=queue,
        clock=lambda: NOW,
    )
    return sessions, service, queue, mapping_id, build_id, node_ids


@pytest.fixture(scope="module")
def postgres_engine():
    if shutil.which("docker") is None:
        pytest.skip("Docker is required for PostgreSQL recipe concurrency tests")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    name = f"vonk-recipe-test-{uuid.uuid4().hex[:12]}"
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


def test_install_is_digest_bound_idempotent_and_gang_complete(tmp_path: Path) -> None:
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    plan = service.preview_install(mapping_id, build_id)
    operation = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="1" * 36
    )
    repeated = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="1" * 36
    )

    assert repeated == operation
    assert operation.kind == "recipe.install"
    assert operation.state == "running"
    assert queue.available == 1
    with sessions() as session:
        jobs = list(session.scalars(select(Job)))
        child_operations = list(session.scalars(select(AgentOperation)))
        assert len(jobs) == 1
        assert {item.kind for item in child_operations} == {"recipe.install"}
        assert all(
            "shell" not in json.dumps(item.payload).lower() for item in child_operations
        )

    service.record_node_result(
        operation.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    assert service.get(operation.id).state == "running"
    service.record_node_result(
        operation.id, nodes[1], succeeded=True, evidence={"installed_bytes": 120}
    )
    completed = service.get(operation.id)
    assert completed.state == "succeeded"
    assert completed.result == {
        "successful_nodes": sorted(nodes),
        "failed_nodes": [],
        "node_evidence": {
            nodes[0]: {"installed_bytes": 120},
            nodes[1]: {"installed_bytes": 120},
        },
    }
    with sessions() as session:
        assert session.get(RecipeInstallation, operation.owner_id).state == "installed"


def test_install_admission_and_queue_creation_roll_back_together(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    service._agent_jobs = FailingQueue()
    plan = service.preview_install(mapping_id, build_id)

    with pytest.raises(RuntimeError, match="queue write failed"):
        service.install(
            plan,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id="0" * 36,
        )

    with sessions() as session:
        assert list(session.scalars(select(RecipeInstallation))) == []
        assert list(session.scalars(select(ResourceReservation))) == []
        assert list(session.scalars(select(Job))) == []


def test_run_admission_and_start_queue_roll_back_together(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="b" * 36,
    )
    service.record_node_result(
        install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    run_plan = service.preview_run(install.owner_id)
    service._agent_jobs = FailingQueue()

    with pytest.raises(RuntimeError, match="queue write failed"):
        service.start(
            run_plan,
            plan_digest=run_plan.plan_digest,
            alias="qwen",
            actor="admin",
            request_id="c" * 36,
        )

    with sessions() as session:
        assert list(session.scalars(select(RecipeRun))) == []
        assert (
            list(
                session.scalars(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_kind == "run"
                    )
                )
            )
            == []
        )
        assert session.scalar(select(Job).where(Job.request_id == "c" * 36)) is None


def test_stop_state_and_queue_creation_roll_back_together(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="1" * 35 + "a",
    )
    service.record_node_result(
        install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    run_plan = service.preview_run(install.owner_id)
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        alias="qwen",
        actor="admin",
        request_id="1" * 35 + "b",
    )
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
        evidence = start_evidence(child.payload)
    service.record_node_result(start.id, nodes[0], succeeded=True, evidence=evidence)
    service._agent_jobs = FailingQueue()

    with pytest.raises(RuntimeError, match="queue write failed"):
        service.stop(start.owner_id, actor="admin", request_id="1" * 35 + "c")

    with sessions() as session:
        assert session.get(RecipeRun, start.owner_id).state == "running"
        assert (
            session.scalar(select(Job).where(Job.request_id == "1" * 35 + "c")) is None
        )


def test_partial_install_fails_as_a_group_and_can_retry(tmp_path: Path) -> None:
    _sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    plan = service.preview_install(mapping_id, build_id)
    first = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 36
    )
    service.record_node_result(
        first.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    service.record_node_result(
        first.id, nodes[1], succeeded=False, evidence={"code": "pull.failed"}
    )

    assert service.get(first.id).state == "failed"
    assert service.get(first.id).result["successful_nodes"] == [nodes[0]]
    retry = service.retry(first.id, actor="admin", request_id="3" * 36)
    assert retry.id != first.id
    assert retry.owner_id == first.owner_id
    with pytest.raises(RecipeOperationConflict, match="not retryable"):
        service.retry(first.id, actor="admin", request_id="3" * 35 + "4")


def test_failed_install_retry_state_rolls_back_when_queue_write_fails(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    plan = service.preview_install(mapping_id, build_id)
    first = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="2" * 35 + "a"
    )
    service.record_node_result(
        first.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    service.record_node_result(
        first.id, nodes[1], succeeded=False, evidence={"code": "pull.failed"}
    )
    with sessions() as session:
        before = {
            node.node_id: node.state
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == first.owner_id
                )
            )
        }
    service._agent_jobs = FailingQueue()

    with pytest.raises(RuntimeError, match="queue write failed"):
        service.retry(first.id, actor="admin", request_id="2" * 35 + "b")

    with sessions() as session:
        assert session.get(RecipeInstallation, first.owner_id).state == "partial"
        after = {
            node.node_id: node.state
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == first.owner_id
                )
            )
        }
        assert after == before


def test_start_stop_and_uninstall_preserve_capacity_safely(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="4" * 36,
    )
    service.record_node_result(
        install.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )

    run_plan = service.preview_run(install.owner_id)
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        alias="qwen",
        actor="admin",
        request_id="5" * 36,
    )
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
        assert child is not None
        assert child.payload["endpoint_address"] == "192.168.1.211"
        assert child.payload["world_size"] == 1
        assert child.payload["master_address"] is None
        evidence = start_evidence(child.payload)
    with pytest.raises(RecipeOperationConflict, match="active run"):
        service.uninstall(install.owner_id, actor="admin", request_id="6" * 36)

    service.record_node_result(
        start.id,
        nodes[0],
        succeeded=True,
        evidence=evidence,
    )
    assert service.get(start.id).state == "succeeded"
    stop = service.stop(start.owner_id, actor="admin", request_id="7" * 36)
    assert service.get(stop.id).state == "running"
    with pytest.raises(RecipeOperationConflict, match="not stoppable"):
        service.stop(start.owner_id, actor="admin", request_id="7" * 35 + "a")
    service.record_node_result(
        stop.id, nodes[0], succeeded=True, evidence={"stopped": True}
    )
    with sessions() as session:
        run = session.get(RecipeRun, start.owner_id)
        reservations = list(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == run.id,
                    ResourceReservation.state == "active",
                )
            )
        )
        assert run.state == "stopped"
        assert reservations == []

    uninstall = service.uninstall(install.owner_id, actor="admin", request_id="8" * 36)
    service.record_node_result(
        uninstall.id, nodes[0], succeeded=True, evidence={"removed": True}
    )
    with sessions() as session:
        installation = session.get(RecipeInstallation, install.owner_id)
        assert installation.state == "uninstalled"


def test_run_status_projects_exact_rank_health_without_agent_secrets(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="9" * 36,
    )
    for node_id in nodes:
        service.record_node_result(
            install.id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id)
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        alias="qwen",
        actor="admin",
        request_id="a" * 36,
    )
    with sessions.begin() as session:
        ranks = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == start.owner_id)
                .order_by(RunNode.rank)
            )
        )
        ranks[0].state = "running"
        ranks[0].evidence_digest = "1" * 64
        ranks[0].updated_at = NOW
        ranks[1].state = "failed"
        ranks[1].evidence_digest = "2" * 64
        ranks[1].updated_at = NOW

    status = service.run_status(start.owner_id)

    assert status.id == start.owner_id
    assert status.alias == "qwen"
    assert status.healthy is False
    assert [rank.state for rank in status.ranks] == ["running", "failed"]
    assert [rank.fresh for rank in status.ranks] == [True, True]
    assert [rank.age_seconds for rank in status.ranks] == [0.0, 0.0]
    with sessions() as session:
        assert all(
            session.get(AgentNode, node_id).state == "active" for node_id in nodes
        )


def test_authenticated_run_observations_project_failure_recovery_and_missing_rank(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="b" * 36,
    )
    for node_id in nodes:
        service.record_node_result(
            install.id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id)
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        alias="observed-gang",
        actor="admin",
        request_id="c" * 36,
    )
    with sessions() as session:
        operations = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == start.id)
                .order_by(AgentOperation.node_id)
            )
        )
    for operation in operations:
        service.record_node_result(
            start.id,
            operation.node_id,
            succeeded=True,
            evidence=start_evidence(operation.payload),
        )

    record_recipe_run_observations(
        sessions,
        nodes[0],
        NOW,
        (RecipeRunObservation(start.owner_id, True),),
    )
    record_recipe_run_observations(sessions, nodes[1], NOW, ())
    failed = service.run_status(start.owner_id)
    assert [rank.state for rank in failed.ranks] == ["running", "failed"]
    assert [rank.fresh for rank in failed.ranks] == [True, True]
    assert failed.healthy is False

    record_recipe_run_observations(
        sessions,
        nodes[1],
        NOW,
        (RecipeRunObservation(start.owner_id, True),),
    )
    recovered = service.run_status(start.owner_id)
    assert [rank.state for rank in recovered.ranks] == ["running", "running"]
    assert recovered.healthy is True


def test_run_observation_snapshot_rejects_duplicate_and_naive_evidence(
    tmp_path: Path,
) -> None:
    sessions, _service, _queue, _mapping_id, _build_id, nodes = setup_services(tmp_path)
    observation = RecipeRunObservation(str(uuid.uuid4()), True)

    with pytest.raises(ValueError, match="duplicated"):
        record_recipe_run_observations(
            sessions, nodes[0], NOW, (observation, observation)
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        record_recipe_run_observations(sessions, nodes[0], NOW.replace(tzinfo=None), ())


def test_multinode_start_is_bound_to_authenticated_fabric_rendezvous(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="d" * 36,
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id)
    assert run_plan.allowed is True
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        alias="qwen-gang",
        actor="admin",
        request_id="e" * 36,
    )

    with sessions() as session:
        children = list(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == start.id)
                .order_by(AgentOperation.node_id)
            )
        )
        assert [child.payload["local_address"] for child in children] == [
            "192.168.100.2",
            "192.168.100.3",
        ]
        assert {child.payload["master_address"] for child in children} == {
            "192.168.100.2"
        }
        assert {child.payload["master_port"] for child in children} == {29500}
        assert {child.payload["world_size"] for child in children} == {2}


def test_failed_multinode_start_queues_idempotent_stop_for_every_rank(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="f" * 35 + "0",
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id)
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        alias="qwen-gang",
        actor="admin",
        request_id="f" * 35 + "1",
    )
    with sessions() as session:
        children = {
            child.node_id: child
            for child in session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        }
        successful_evidence = start_evidence(children[nodes[0]].payload)
    service.record_node_result(
        start.id, nodes[0], succeeded=True, evidence=successful_evidence
    )
    service.record_node_result(
        start.id, nodes[1], succeeded=False, evidence={"code": "start.failed"}
    )

    with sessions() as session:
        cleanup = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == start.owner_id,
            )
        )
        assert cleanup is not None
        cleanup_nodes = set(
            session.scalars(
                select(AgentOperation.node_id).where(
                    AgentOperation.parent_job_id == cleanup.id
                )
            )
        )
        assert cleanup_nodes == set(nodes)
        assert session.get(RecipeRun, start.owner_id).state == "stopping"


def test_concurrent_final_rank_results_serialize_gang_cleanup(
    tmp_path: Path, postgres_engine
) -> None:
    Base.metadata.drop_all(postgres_engine)
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, engine=postgres_engine
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="d" * 36,
    )
    for node in nodes:
        service.record_node_result(
            install.id, node, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id)
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        alias="qwen-gang",
        actor="admin",
        request_id="e" * 36,
    )
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == start.id,
                AgentOperation.node_id == nodes[0],
            )
        )
        assert child is not None
        evidence = start_evidence(child.payload)
    barrier = threading.Barrier(2)

    def result(node_id: str, succeeded: bool) -> None:
        barrier.wait()
        service.record_node_result(
            start.id,
            node_id,
            succeeded=succeeded,
            evidence=evidence if succeeded else {"code": "start.failed"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(result, nodes[0], True),
            pool.submit(result, nodes[1], False),
        ]
        for future in futures:
            future.result(timeout=10)

    with sessions() as session:
        cleanup = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == start.owner_id,
            )
        )
        assert cleanup is not None
        assert session.get(RecipeRun, start.owner_id).state == "stopping"
        assert set(
            session.scalars(
                select(AgentOperation.node_id).where(
                    AgentOperation.parent_job_id == cleanup.id
                )
            )
        ) == set(nodes)


def test_changed_plan_or_reused_request_key_is_rejected(tmp_path: Path) -> None:
    _sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping_id, build_id)
    with pytest.raises(RecipeOperationConflict, match="plan digest"):
        service.install(plan, plan_digest="0" * 64, actor="admin", request_id="9" * 36)
    service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="a" * 36
    )
    with pytest.raises(RecipeOperationConflict, match="request key"):
        service.stop("f" * 36, actor="admin", request_id="a" * 36)
