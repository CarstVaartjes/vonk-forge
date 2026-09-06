from __future__ import annotations

import hashlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentOperation as ProtocolAgentOperation
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
    Observation,
    RecipeBuild,
    ResourceReservation,
)
from vonk_control.recipe_operations import RecipeOperationService

from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    claim_agent,
)

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
COMMIT = "a" * 64
PROBE_RESULT = {
    "status": "ok",
    "evidence": {
        "vonk_forge": {
            "schema_version": 1,
            "memory": {"available_bytes": 1_000},
            "storage": {"available_bytes": 2_000},
            "accelerator": {
                "available": True,
                "active_nvidia_compute_processes": 0,
            },
        },
        "nvidia": {"tools": {}},
    },
}


@pytest.mark.parametrize(
    ("count", "occupancy"),
    ((0, "clean"), (2, "active"), (None, "unknown")),
)
def test_probe_persists_bounded_compute_occupancy(
    count: int | None, occupancy: str
) -> None:
    result = {
        "status": "ok",
        "evidence": {
            "vonk_forge": {
                "schema_version": 1,
                "memory": {"available_bytes": 1_000},
                "storage": {"available_bytes": 2_000},
                "accelerator": {
                    "available": True,
                    "active_nvidia_compute_processes": count,
                },
            },
            "nvidia": {"tools": {}},
        },
    }

    health = AgentJobService._probe_health(result)

    assert health["active_nvidia_compute_processes"] == count
    assert health["compute_occupancy"] == occupancy
    assert health["status"] == ("warning" if occupancy == "unknown" else "healthy")


@pytest.mark.parametrize("total", [999, -1, True, "4000"])
def test_probe_total_capacity_must_be_bounded_and_cover_available(
    total: object,
) -> None:
    result = {
        "status": "ok",
        "evidence": {
            "vonk_forge": {
                "schema_version": 1,
                "memory": {"available_bytes": 1_000, "total_bytes": total},
                "storage": {"available_bytes": 2_000, "total_bytes": 8_000},
                "accelerator": {"available": True},
            },
            "nvidia": {"tools": {}},
        },
    }

    with pytest.raises(ValueError, match="capacity"):
        AgentJobService._probe_health(result)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def service(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent-jobs.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    clock = Clock()
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    capabilities=[],
                    architecture="linux-arm64",
                    semantic_version="1.0.0",
                    build_digest="sha256:" + "f" * 64,
                    binary_digest="f" * 64,
                    self_test_passed=True,
                )
            )
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=clock.now - timedelta(seconds=1),
                    not_after=clock.now + timedelta(hours=1),
                    fingerprint=f"fingerprint-{serial}",
                )
            )
    return AgentJobService(sessions, clock=clock), sessions, clock


def parent(sessions, clock) -> Job:
    job = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        authority_revision=COMMIT,
        targets=[NODE_A, NODE_B],
        payload_digest=hashlib.sha256(b"{}").hexdigest(),
        payload={},
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(job)
    return job


def job_state(sessions, job_id: str) -> Job:
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def test_agent_can_claim_only_its_node_operation(service) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {}
    )

    assert claim_agent(jobs, NODE_B, "serial-b", 30) is None
    claim = claim_agent(jobs, NODE_A, "serial-a", 30)

    assert claim is not None
    assert claim.operation_id == operation.id
    assert claim.node_id == NODE_A


def test_agent_upgrade_completes_only_after_exact_new_runtime_reconnects(
    service,
) -> None:
    jobs, sessions, clock = service
    target = {
        "architecture": "linux-arm64",
        "package_bytes": 1234,
        "package_sha256": "d" * 64,
        "package_signature": "e" * 128,
        "package_url": (
            "https://install.vonkforge.ai/dev/releases/example/"
            "spark/current/linux-arm64/vonk-forge-agent.deb"
        ),
        "package_version": "0.1.0~dev.330+g0123456789ab",
        "schema_version": 1,
        "target_binary_digest": "a" * 64,
        "target_build_digest": "sha256:" + "b" * 64,
    }
    job = parent(sessions, clock)
    operation = jobs.enqueue(job.id, NODE_A, "agent.upgrade.v1", COMMIT, target)
    old_identity = {
        "architecture": "linux-arm64",
        "binary_digest": "f" * 64,
        "build_digest": "sha256:" + "f" * 64,
        "semantic_version": "0.1.0",
        "self_test_passed": True,
    }
    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
        30,
        capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
        runtime_identity=old_identity,
    )
    assert claim is not None
    assert claim.operation_id == operation.id
    assert job_state(sessions, job.id).state == "queued"

    new_identity = {
        **old_identity,
        "binary_digest": target["target_binary_digest"],
        "build_digest": target["target_build_digest"],
    }
    assert (
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            30,
            capabilities=["agent.runtime.rust.v1", "agent.upgrade.v1"],
            runtime_identity=new_identity,
        )
        is None
    )

    assert job_state(sessions, job.id).state == "succeeded"
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
        assert stored is not None and stored.state == "succeeded"
        assert attempt is not None and attempt.state == "succeeded"
        assert attempt.result == {
            "architecture": "linux-arm64",
            "binary_digest": "a" * 64,
            "build_digest": "sha256:" + "b" * 64,
            "package_sha256": "d" * 64,
            "package_version": "0.1.0~dev.330+g0123456789ab",
            "self_test_passed": True,
            "status": "upgraded",
        }


def test_rust_node_cannot_be_assigned_an_unadvertised_operation(service) -> None:
    jobs, sessions, clock = service
    job = parent(sessions, clock)
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.capabilities = ["recipe.install"]

    with pytest.raises(ValueError, match="does not advertise"):
        jobs.enqueue(job.id, NODE_A, "node.probe", COMMIT, {})

    stored = jobs.enqueue(
        job.id,
        NODE_A,
        "recipe.install",
        COMMIT,
        {"schema_version": 1, "recipe": {}},
    )
    assert stored.kind == "recipe.install"


def test_artifact_distribution_is_negotiated_and_serialized_as_a_mutation(
    service,
) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    operation = ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value
    first = jobs.enqueue(parent_job.id, NODE_A, operation, COMMIT, {})
    second = jobs.enqueue(parent_job.id, NODE_A, operation, COMMIT, {})
    capabilities = ["agent.runtime.rust.v1", operation]

    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
        30,
        protocol_version=3,
        capabilities=capabilities,
    )

    assert claim is not None
    assert claim.operation_id == first.id
    assert (
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            capabilities=capabilities,
        )
        is None
    )
    with sessions() as session:
        stored = session.get(AgentOperation, second.id)
        assert stored is not None and stored.state == "queued"


def test_rust_claim_updates_current_contact(service) -> None:
    jobs, _sessions, _clock = service
    assert (
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            capabilities=["agent.runtime.rust.v1", "recipe.install"],
            runtime_identity=PACKAGED_RUNTIME_IDENTITY,
        )
        is None
    )


def test_service_claim_requires_packaged_runtime_identity_with_rust_capability(
    service,
) -> None:
    jobs, _sessions, _clock = service

    with pytest.raises(TypeError, match="runtime_identity"):
        jobs.claim(
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            capabilities=["agent.runtime.rust.v1", "recipe.install"],
        )

    with pytest.raises(ValueError, match="runtime identity"):
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            capabilities=["agent.runtime.rust.v1", "recipe.install"],
            runtime_identity=None,
        )


def test_service_claim_requires_rust_capability_with_packaged_runtime_identity(
    service,
) -> None:
    jobs, _sessions, _clock = service

    with pytest.raises(ValueError, match="capability negotiation"):
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            capabilities=["recipe.install"],
            runtime_identity=PACKAGED_RUNTIME_IDENTITY,
        )


def test_signed_observation_receipt_key_is_bound_on_upgrade_and_immutable(
    service,
) -> None:
    jobs, sessions, _clock = service
    receipt_identity = {
        **PACKAGED_RUNTIME_IDENTITY,
        "observation_receipt_public_key": "1" * 64,
    }

    assert (
        jobs.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=[
                "agent.runtime.rust.v1",
                "recipe.run.inspect.receipt.v1",
            ],
            runtime_identity=receipt_identity,
        )
        is None
    )
    with sessions() as session:
        assert (
            session.get(AgentNode, NODE_A).observation_receipt_public_key == "1" * 64
        )

    with pytest.raises(ValueError, match="receipt key changed"):
        jobs.claim(
            NODE_A,
            "serial-a",
            30,
            capabilities=[
                "agent.runtime.rust.v1",
                "recipe.run.inspect.receipt.v1",
            ],
            runtime_identity={
                **receipt_identity,
                "observation_receipt_public_key": "2" * 64,
            },
        )
    with pytest.raises(ValueError, match="receipt identity is incomplete"):
        jobs.claim(
            NODE_B,
            "serial-b",
            30,
            capabilities=[
                "agent.runtime.rust.v1",
                "recipe.run.inspect.receipt.v1",
            ],
            runtime_identity=PACKAGED_RUNTIME_IDENTITY,
        )


def test_package_operation_is_not_a_control_plane_queue_operation(service) -> None:
    jobs, sessions, clock = service

    with pytest.raises(ValueError, match="not supported"):
        jobs.enqueue(
            parent(sessions, clock).id,
            NODE_A,
            "package.prepare",
            COMMIT,
            {
                "schema_version": 1,
                "deployment_id": "removed-package",
                "release_digest": "a" * 64,
                "deployment_digest": "b" * 64,
            },
        )


def test_package_capabilities_are_not_control_plane_agent_capabilities(
    service,
) -> None:
    jobs, _sessions, _clock = service

    with pytest.raises(ValueError, match="capability negotiation is incomplete"):
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            capabilities=[
                "node.probe",
                "release.install",
                "workload.health",
                "workload.prepare",
                "workload.start",
                "workload.stop",
                "workload.verify",
                "package.prepare",
            ],
        )


def test_recipe_only_agent_is_not_forced_to_advertise_old_executors(service) -> None:
    jobs, sessions, clock = service
    queued = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        "recipe.install",
        COMMIT,
        {
            "schema_version": 1,
            "installation_id": "00000000-0000-4000-8000-000000000001",
            "recipe_revision_id": "00000000-0000-4000-8000-000000000002",
            "recipe_content_sha256": "a" * 64,
            "plan_digest": "b" * 64,
            "expected_bytes": 100,
        },
    )

    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
        30,
        protocol_version=3,
        capabilities=["agent.runtime.rust.v1", "recipe.install"],
    )

    assert claim is not None
    assert claim.operation.value == queued.kind == "recipe.install"


def test_recipe_build_is_rejected_when_builder_runtime_changed_before_claim(
    service,
) -> None:
    jobs, sessions, clock = service
    build_id = "00000000-0000-4000-8000-000000000009"
    revision_id = "00000000-0000-4000-8000-000000000001"
    payload = {
        "schema_version": 1,
        "kind": "recipe.build.v1",
        "build_id": build_id,
        "recipe_revision_id": revision_id,
        "recipe_content_sha256": "a" * 64,
        "source_bundle_sha256": "b" * 64,
        "source_bundle_bytes": 4096,
        "base_images": [],
        "base_image_storage_bytes": 0,
        "capabilities": [],
        "build_input_sha256": "c" * 64,
        "dockerfile": "Dockerfile",
        "platform": "linux/arm64",
        "arguments": [],
        "network": {"mode": "none", "hosts": []},
        "options": {"additional_contexts": [], "annotations": [], "environment": [], "format": "oci", "identity_label": True, "ignorefile": None, "jobs": 1, "labels": [], "layer_compression": "disabled", "layer_labels": [], "layers": True, "no_hostname": False, "no_hosts": False, "omit_history": False, "os_features": [], "os_version": None, "shm_bytes": 67108864, "skip_unused_stages": True, "squash": "none", "timestamp": None, "unset_environment": [], "unset_labels": []},
        "target": None,
        "limits": {
            "cpu_cores": 8,
            "memory_bytes": 1024,
            "temporary_bytes": 4096,
            "processes": 64,
            "timeout_seconds": 600,
            "output_bytes": 2048,
            "gpu": 0,
            "privileged": False,
            "host_mounts": False,
            "container_socket": False,
        },
    }
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id=revision_id,
                builder_node_id=NODE_A,
                source_bundle_sha256="b" * 64,
                build_input_sha256="c" * 64,
                state="building",
                policy_report={
                    "passed": True,
                    "builder_binary_digest": "f" * 64,
                    "artifact_format": "docker-archive-v1",
                },
                plan=payload,
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            ResourceReservation(
                node_id=NODE_A,
                kind="disk",
                resource_key="c" * 64,
                amount_bytes=4096,
                owner_kind="recipe-build",
                owner_id=build_id,
                state="active",
                plan_digest="c" * 64,
                created_at=clock.now,
            )
        )
        stored_parent = session.get(Job, parent_job.id)
        assert stored_parent is not None
        stored_parent.kind = "recipe.build.v1"
        stored_parent.payload = {
            "schema_version": 1,
            "owner_kind": "recipe-build",
            "owner_id": build_id,
            "plan_digest": "c" * 64,
        }
    recipe_operations = RecipeOperationService(
        sessions,
        install_admission=object(),
        run_admission=object(),
        agent_jobs=jobs,
        clock=clock,
    )
    jobs.set_result_consumer(recipe_operations.consume_agent_result)
    operation = jobs.enqueue(
        parent_job.id,
        NODE_A,
        "recipe.build.v1",
        COMMIT,
        payload,
    )
    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
        30,
        protocol_version=3,
        capabilities=["agent.runtime.rust.v1", "recipe.build.v1"],
        runtime_identity={
            "architecture": "linux-arm64",
            "build_digest": "sha256:" + "e" * 64,
            "binary_digest": "e" * 64,
            "semantic_version": "1.2.3",
            "self_test_passed": True,
        },
    )

    assert claim is None
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        parent_row = session.get(Job, parent_job.id)
        node = session.get(AgentNode, NODE_A)
        build = session.get(RecipeBuild, build_id)
        reservation = session.scalar(
            select(ResourceReservation).where(ResourceReservation.owner_id == build_id)
        )
        assert stored is not None and stored.state == "failed"
        assert parent_row is not None and parent_row.state == "failed"
        assert node is not None and node.binary_digest == "e" * 64
        assert build is not None and build.state == "failed"
        assert reservation is not None and reservation.state == "released"


def test_recipe_build_requires_runtime_identity_on_the_current_claim(service) -> None:
    jobs, sessions, clock = service
    build_id = "00000000-0000-4000-8000-000000000019"
    revision_id = "00000000-0000-4000-8000-000000000011"
    payload = {
        "schema_version": 1,
        "kind": "recipe.build.v1",
        "build_id": build_id,
        "recipe_revision_id": revision_id,
        "recipe_content_sha256": "a" * 64,
        "source_bundle_sha256": "b" * 64,
        "source_bundle_bytes": 4096,
        "base_images": [],
        "base_image_storage_bytes": 0,
        "capabilities": [],
        "build_input_sha256": "c" * 64,
        "dockerfile": "Dockerfile",
        "platform": "linux/arm64",
        "arguments": [],
        "network": {"mode": "none", "hosts": []},
        "options": {"additional_contexts": [], "annotations": [], "environment": [], "format": "oci", "identity_label": True, "ignorefile": None, "jobs": 1, "labels": [], "layer_compression": "disabled", "layer_labels": [], "layers": True, "no_hostname": False, "no_hosts": False, "omit_history": False, "os_features": [], "os_version": None, "shm_bytes": 67108864, "skip_unused_stages": True, "squash": "none", "timestamp": None, "unset_environment": [], "unset_labels": []},
        "target": None,
        "limits": {
            "cpu_cores": 8,
            "memory_bytes": 1024,
            "temporary_bytes": 4096,
            "processes": 64,
            "timeout_seconds": 600,
            "output_bytes": 2048,
            "gpu": 0,
            "privileged": False,
            "host_mounts": False,
            "container_socket": False,
        },
    }
    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id=revision_id,
                builder_node_id=NODE_A,
                source_bundle_sha256="b" * 64,
                build_input_sha256="c" * 64,
                state="building",
                policy_report={
                    "passed": True,
                    "builder_binary_digest": "f" * 64,
                    "artifact_format": "docker-archive-v1",
                },
                plan=payload,
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(
        parent_job.id,
        NODE_A,
        "recipe.build.v1",
        COMMIT,
        payload,
    )

    claim = claim_agent(
        jobs,
        NODE_A,
        "serial-a",
        30,
        protocol_version=3,
        capabilities=["agent.runtime.rust.v1", "recipe.build.v1"],
    )

    assert claim is None
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state == "failed"


@pytest.mark.parametrize("count", (2, None))
def test_control_rejects_success_for_unsatisfied_zero_compute_gate(
    service, count: int | None
) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        "node.probe",
        COMMIT,
        {"require_active_nvidia_compute_processes": 0},
    )
    claim = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert claim is not None
    result = deepcopy(PROBE_RESULT)
    result["evidence"]["vonk_forge"]["accelerator"][
        "active_nvidia_compute_processes"
    ] = count

    with pytest.raises(ValueError, match="compute gate"):
        jobs.succeed(claim, result)

    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state == "running"
        assert session.scalars(select(Observation)).all() == []


def test_concurrent_agents_cannot_claim_the_same_operation(service) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {}
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(
            pool.map(lambda _: claim_agent(jobs, NODE_A, "serial-a", 30), range(4))
        )

    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    assert claimed[0].operation_id == operation.id


def test_long_poll_wakes_on_enqueue_and_times_out_without_per_client_state(
    service,
) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(claim_agent, jobs, NODE_A, "serial-a", 30, 1.0)
        time.sleep(0.05)
        operation = jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
        claim = waiting.result(timeout=1)
    elapsed = time.monotonic() - started

    assert claim is not None and claim.operation_id == operation.id
    assert elapsed < 0.8

    timeout_started = time.monotonic()
    assert claim_agent(jobs, NODE_B, "serial-b", 30, 0.08) is None
    timeout_elapsed = time.monotonic() - timeout_started
    # The lower bound proves the requested long-poll timeout is honored. Keep
    # the upper bound generous enough for a CPU-starved parallel CI worker to
    # be scheduled after the condition deadline has already elapsed.
    assert 0.06 <= timeout_elapsed < 1.5


def test_long_poll_rechecks_database_for_another_process_enqueue(service) -> None:
    jobs, sessions, clock = service
    other_process = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    first_poll = Event()
    original_claim_once = jobs._claim_once

    def observed_claim_once(*args, **kwargs):
        result = original_claim_once(*args, **kwargs)
        first_poll.set()
        return result

    jobs._claim_once = observed_claim_once  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(claim_agent, jobs, NODE_A, "serial-a", 30, 2.0)
        assert first_poll.wait(timeout=1)
        operation = other_process.enqueue(
            parent_job.id, NODE_A, "node.probe", COMMIT, {}
        )
        claim = waiting.result(timeout=0.8)

    assert claim is not None and claim.operation_id == operation.id


def test_expired_attempt_cannot_publish_success(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    first = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert first is not None

    clock.advance(seconds=31)
    second = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert second is not None

    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(first, PROBE_RESULT)
    jobs.succeed(second, PROBE_RESULT)


def test_revoked_expired_or_node_mismatched_certificate_cannot_claim(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    with sessions.begin() as session:
        session.get(AgentCertificate, "serial-a").revoked_at = clock.now  # type: ignore[union-attr]

    assert claim_agent(jobs, NODE_A, "serial-a", 30) is None
    assert claim_agent(jobs, NODE_A, "serial-b", 30) is None

    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.revoked_at = None
        certificate.not_after = clock.now

    assert claim_agent(jobs, NODE_A, "serial-a", 30) is None


def test_enqueue_rejects_noncanonical_protocol_payload(service) -> None:
    jobs, sessions, clock = service

    with pytest.raises(ValueError, match="unsafe|protocol"):
        jobs.enqueue(
            parent(sessions, clock).id,
            NODE_A,
            "node.probe",
            COMMIT,
            {"command": "uname"},
        )
    with pytest.raises(ValueError, match="large|protocol"):
        jobs.enqueue(
            parent(sessions, clock).id,
            NODE_A,
            "node.probe",
            COMMIT,
            {"value": "x" * 70_000},
        )


@pytest.mark.parametrize(
    "terminal_state", ("succeeded", "failed", "waiting-for-operator", "expired")
)
def test_sqlite_enqueue_rejects_terminal_parent(service, terminal_state: str) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        session.get(Job, parent_job.id).state = terminal_state  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="terminal"):
        jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})


def test_sqlite_enqueue_enforces_parent_commit_and_target(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)

    with pytest.raises(ValueError, match="authority revision"):
        jobs.enqueue(parent_job.id, NODE_A, "node.probe", "b" * 64, {})
    with sessions.begin() as session:
        stored_parent = session.get(Job, parent_job.id)
        assert stored_parent is not None
        stored_parent.targets = [NODE_A]
    with pytest.raises(ValueError, match="target"):
        jobs.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})


def test_sqlite_enqueue_rejects_retired_node_before_parent_mutation(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.state = "retired"
        node.revoked_at = clock.now

    with pytest.raises(ValueError, match="active"):
        jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})

    assert job_state(sessions, parent_job.id).state == "queued"


def test_heartbeat_persists_canonical_progress_and_renews_lease(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    claim = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert claim is not None

    progress = jobs.heartbeat(claim, {"phase": "checking"}, 60)

    assert progress.deadline > claim.deadline
    assert progress.cancel_requested is False
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim.fence
            )
        )
        assert attempt is not None
        assert attempt.progress == {"phase": "checking"}


def test_heartbeat_never_shortens_a_longer_existing_lease(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    claim = claim_agent(jobs, NODE_A, "serial-a", 120)
    assert claim is not None
    clock.advance(seconds=10)

    progress = jobs.heartbeat(claim, {"phase": "checking"}, 30)

    assert progress.deadline >= claim.deadline


def test_claim_persists_authenticated_running_release_identity(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    runtime_identity = {
        "architecture": "linux-arm64",
        "binary_digest": "c" * 64,
        "build_digest": "sha256:" + "c" * 64,
        "semantic_version": "1.2.3",
        "self_test_passed": True,
    }

    assert (
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            runtime_identity=runtime_identity,
        )
        is not None
    )

    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert {
            "architecture": node.architecture,
            "binary_digest": node.binary_digest,
            "build_digest": node.build_digest,
            "semantic_version": node.semantic_version,
            "self_test_passed": node.self_test_passed,
        } == runtime_identity


@pytest.mark.parametrize("architecture", ("linux-riscv64", True, 7))
def test_claim_rejects_malformed_runtime_architecture_without_persisting_it(
    service, architecture: object
) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    runtime_identity = {
        "architecture": architecture,
        "binary_digest": "c" * 64,
        "build_digest": "sha256:" + "c" * 64,
        "semantic_version": "1.2.3",
        "self_test_passed": True,
    }

    with pytest.raises(ValueError, match="runtime identity"):
        claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            runtime_identity=runtime_identity,
        )

    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.architecture == "linux-arm64"
        assert node.semantic_version == "1.0.0"
        assert node.build_digest == "sha256:" + "f" * 64
        assert node.binary_digest == "f" * 64
        assert node.self_test_passed is True


@pytest.mark.parametrize("agent_action", ("heartbeat", "result"))
def test_retired_identity_cannot_mutate_active_attempt_or_record_contact(
    service, agent_action: str
) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {}
    )
    claim = claim_agent(jobs, NODE_A, "serial-a", 30, protocol_version=3)
    assert claim is not None
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        certificate = session.get(AgentCertificate, "serial-a")
        assert node is not None and certificate is not None
        node.state = "retired"
        node.revoked_at = clock.now
        node.last_seen_at = None
        certificate.state = "revoked"
        certificate.revoked_at = clock.now

    with pytest.raises(StaleAgentAttempt):
        if agent_action == "heartbeat":
            jobs.heartbeat(claim, {"phase": "checking"}, 60)
        else:
            jobs.succeed(claim, {"healthy": True})

    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        stored_operation = session.get(AgentOperation, operation.id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == claim.attempt,
            )
        )
        assert node is not None and node.last_seen_at is None
        assert node.protocol_version == 3
        assert stored_operation is not None and stored_operation.state == "running"
        assert attempt is not None and attempt.state == "running"
        assert attempt.progress is None and attempt.result is None


def test_public_fence_string_interface_renews_and_completes(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    claim = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert claim is not None

    progress = jobs.heartbeat(claim.fence, {"phase": "checking"}, 60)
    jobs.succeed(progress.fence, PROBE_RESULT)

    with pytest.raises(StaleAgentAttempt):
        jobs.fail(str(uuid.uuid4()), "unknown fence")


def test_structured_fence_cannot_update_a_different_operation(service) -> None:
    jobs, sessions, clock = service
    first_operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {}
    )
    second_operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {}
    )
    first = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert first is not None
    other_operation = (
        second_operation
        if first.operation_id == first_operation.id
        else first_operation
    )
    forged = type(first)(**{**first.__dict__, "operation_id": other_operation.id})
    with pytest.raises(StaleAgentAttempt):
        jobs.heartbeat(forged, {"phase": "forged"}, 30)
    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(forged, PROBE_RESULT)
    assert first.operation_id != other_operation.id


def test_attempt_expiring_exactly_at_claim_time_is_reclaimable(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    first = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert first is not None

    clock.advance(seconds=30)
    second = claim_agent(jobs, NODE_A, "serial-a", 30)

    assert second is not None
    assert second.fence != first.fence


def test_parent_job_becomes_succeeded_only_after_every_operation_succeeds(
    service,
) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
    jobs.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})

    first = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert first is not None
    jobs.succeed(first, PROBE_RESULT)
    assert job_state(sessions, parent_job.id).state == "queued"

    second = claim_agent(jobs, NODE_B, "serial-b", 30)
    assert second is not None
    jobs.succeed(second, PROBE_RESULT)

    assert job_state(sessions, parent_job.id).state == "succeeded"


def test_parent_job_fails_when_all_operations_are_terminal_and_one_failed(
    service,
) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
    jobs.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})

    failed = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert failed is not None
    jobs.fail(failed, "token=sensitive " + "x" * 2_000)
    assert job_state(sessions, parent_job.id).state == "queued"

    succeeded = claim_agent(jobs, NODE_B, "serial-b", 30)
    assert succeeded is not None
    jobs.succeed(succeeded, PROBE_RESULT)

    aggregate = job_state(sessions, parent_job.id)
    assert aggregate.state == "failed"
    assert aggregate.status_reason is not None
    assert "sensitive" not in aggregate.status_reason
    assert len(aggregate.status_reason) <= 1024


def test_parent_job_waits_when_all_operations_terminal_without_failures(
    service,
) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
    jobs.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})

    waiting = claim_agent(jobs, NODE_A, "serial-a", 30)
    assert waiting is not None
    jobs.wait_for_operator(waiting, "confirm displayed fingerprint")

    succeeded = claim_agent(jobs, NODE_B, "serial-b", 30)
    assert succeeded is not None
    jobs.succeed(succeeded, PROBE_RESULT)

    assert job_state(sessions, parent_job.id).state == "waiting-for-operator"
