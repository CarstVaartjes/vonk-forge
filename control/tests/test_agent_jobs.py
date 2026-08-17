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
from vonk_agent_protocol import canonical_message
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.models import (
    AgentCertificate,
    AgentEnrollment,
    AgentEnrollmentGrant,
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

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
COMMIT = "a" * 40
PLATFORM_TARGET = "platform/releases/1.2.3/" + "c" * 64 + ".json"
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


class UpdateAuthority:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def refresh_and_validate(self, payload, *, target_name):
        if self.fail:
            raise RuntimeError("signing unavailable")
        assert target_name == PLATFORM_TARGET
        return {"payload_digest": hashlib.sha256(canonical_message(payload)).hexdigest()}

    def authorize(self, payload, **bindings):
        if self.fail:
            raise RuntimeError("signing unavailable")
        self.calls.append({"payload": payload, **bindings})
        artifact = payload["artifact"]
        release = payload["release"]
        receipt = {
            "architecture": artifact["architecture"],
            "attempt": bindings["attempt"],
            "build_digest": release["build_digest"],
            "claim_deadline": bindings["claim_deadline"],
            "expires_at": bindings["expires_at"],
            "fence": bindings["fence"],
            "node_id": bindings["node_id"],
            "oci_manifest_digest": artifact["oci_manifest_digest"],
            "operation_id": bindings["operation_id"],
            "payload_name": artifact["payload_name"],
            "platform_target_name": PLATFORM_TARGET,
            "platform_target_sha256": "c" * 64,
            "platform_version": release["platform_version"],
            "previous_sha256": bindings["previous_sha256"],
            "previous_generation": bindings["previous_generation"],
            "previous_slot": bindings["previous_slot"],
            "sha256": artifact["payload_sha256"],
            "size": artifact["payload_size"],
            "target_slot": "B" if bindings["previous_slot"] == "A" else "A",
            "tuf_targets_version": 7,
        }
        return {
            "artifact": artifact,
            "receipt": receipt,
            "release": release,
            "signature": {
                "algorithm": "ed25519",
                "key_id": "d" * 64,
                "value": "e" * 128,
            },
        }

    def authorize_rollback(self, **bindings):
        if self.fail:
            raise RuntimeError("signing unavailable")
        self.calls.append({"rollback": True, **bindings})
        receipt = {
            "action": "operator-rollback",
            "attempt": bindings["attempt"],
            "claim_deadline": bindings["claim_deadline"],
            "current_generation": bindings["current_generation"],
            "current_sha256": bindings["current_sha256"],
            "current_slot": bindings["current_slot"],
            "expires_at": bindings["expires_at"],
            "fence": bindings["fence"],
            "node_id": bindings["node_id"],
            "operation_id": bindings["operation_id"],
        }
        return {
            "receipt": receipt,
            "signature": {
                "algorithm": "ed25519",
                "key_id": "d" * 64,
                "value": "e" * 128,
            },
        }


def update_payload() -> dict[str, object]:
    return {
        "artifact": {
            "architecture": "linux-arm64",
            "oci_manifest_digest": "sha256:" + "a" * 64,
            "payload_name": "vonk-agent",
            "payload_sha256": "b" * 64,
            "payload_size": 4096,
        },
        "release": {
            "build_digest": "sha256:" + "c" * 64,
            "platform_version": "1.2.3",
            "protocol_maximum": 2,
            "protocol_minimum": 1,
        },
    }


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
            session.add(AgentNode(
                node_id=node_id,
                state="active",
                capabilities=[],
                active_slot="A",
                agent_sha256="f" * 64,
                supervisor_generation=1,
            ))
            session.add(AgentCertificate(
                serial=serial,
                node_id=node_id,
                not_before=clock.now - timedelta(seconds=1),
                not_after=clock.now + timedelta(hours=1),
                fingerprint=f"fingerprint-{serial}",
            ))
    return AgentJobService(sessions, clock=clock), sessions, clock


def parent(sessions, clock) -> Job:
    job = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="operator",
        base_commit=COMMIT,
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
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})

    assert jobs.claim(NODE_B, "serial-b", 30) is None
    claim = jobs.claim(NODE_A, "serial-a", 30)

    assert claim is not None
    assert claim.operation_id == operation.id
    assert claim.node_id == NODE_A


def test_rust_node_cannot_be_assigned_an_unadvertised_operation(service) -> None:
    jobs, sessions, clock = service
    job = parent(sessions, clock)
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.agent_implementation = "rust"
        node.migration_state = "complete"
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


def test_new_enrollment_requires_rust_and_cannot_downgrade_after_migration(service) -> None:
    jobs, sessions, _clock = service
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.agent_implementation = "pending"
        node.migration_state = "required"

    with pytest.raises(ValueError, match="requires the Rust agent"):
        jobs.claim(
            NODE_A,
            "serial-a",
            30,
            protocol_version=2,
            capabilities=["recipe.install"],
            agent_implementation="python",
        )

    assert jobs.claim(
        NODE_A,
        "serial-a",
        30,
        protocol_version=3,
        capabilities=["agent.runtime.rust.v1", "recipe.install"],
        agent_implementation="rust",
    ) is None
    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.agent_implementation == "rust"
        assert node.migration_state == "complete"

    with pytest.raises(ValueError, match="cannot downgrade"):
        jobs.claim(
            NODE_A,
            "serial-a",
            30,
            protocol_version=2,
            capabilities=["recipe.install"],
            agent_implementation="python",
        )


def test_python_cutover_requires_migration_certificate_and_retires_old_identity(
    service,
) -> None:
    jobs, sessions, clock = service
    rust_serial = "serial-rust"

    with pytest.raises(ValueError, match="dedicated migration certificate"):
        jobs.claim(
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            capabilities=["agent.runtime.rust.v1", "recipe.install"],
            agent_implementation="rust",
        )

    grant_id = str(uuid.uuid4())
    with sessions.begin() as session:
        session.add(
            AgentEnrollmentGrant(
                id=grant_id,
                node_id=NODE_A,
                purpose="rust-migration",
                token_digest="d" * 64,
                created_by="admin",
                created_at=clock.now,
                expires_at=clock.now + timedelta(minutes=10),
                consumed_at=clock.now,
            )
        )
        session.add(
            AgentCertificate(
                serial=rust_serial,
                node_id=NODE_A,
                not_before=clock.now - timedelta(seconds=1),
                not_after=clock.now + timedelta(hours=1),
                fingerprint="fingerprint-rust",
                generation=2,
            )
        )
        session.add(
            AgentEnrollment(
                id=str(uuid.uuid4()),
                grant_id=grant_id,
                node_id=NODE_A,
                state="approved",
                csr_pem="csr",
                csr_public_key_pem="public",
                csr_public_key_fingerprint="e" * 64,
                host_key_fingerprint="host",
                hardware_fingerprint="hardware",
                agent_digest="f" * 64,
                boot_id="boot",
                created_at=clock.now,
                decision_actor="admin",
                decided_at=clock.now,
                certificate_pem="certificate",
                chain_pem="chain",
                certificate_serial=rust_serial,
                certificate_fingerprint="fingerprint-rust",
                certificate_generation=2,
                certificate_not_before=clock.now - timedelta(seconds=1),
                certificate_not_after=clock.now + timedelta(hours=1),
            )
        )

    assert (
        jobs.claim(
            NODE_A,
            rust_serial,
            30,
            protocol_version=3,
            capabilities=["agent.runtime.rust.v1", "recipe.install"],
            agent_implementation="rust",
        )
        is None
    )
    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        old = session.get(AgentCertificate, "serial-a")
        new = session.get(AgentCertificate, rust_serial)
        assert node is not None and node.agent_implementation == "rust"
        assert node.migration_state == "complete"
        assert old is not None and old.state == "revoked"
        assert old.revoked_at is not None
        assert old.revoked_at.replace(tzinfo=UTC) == clock.now
        assert new is not None and new.state == "active" and new.revoked_at is None

    with pytest.raises(ValueError, match="cannot downgrade"):
        jobs.claim(
            NODE_A,
            rust_serial,
            30,
            protocol_version=2,
            capabilities=["recipe.install"],
            agent_implementation="python",
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
                "deployment_id": "legacy-package",
                "release_digest": "a" * 64,
                "deployment_digest": "b" * 64,
            },
        )


def test_package_capabilities_are_not_control_plane_agent_capabilities(
    service,
) -> None:
    jobs, _sessions, _clock = service

    with pytest.raises(ValueError, match="agent capabilities"):
        jobs.claim(
            NODE_A,
            "serial-a",
            30,
            protocol_version=2,
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


def test_recipe_only_agent_is_not_forced_to_advertise_legacy_executors(service) -> None:
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

    claim = jobs.claim(
        NODE_A,
        "serial-a",
        30,
        protocol_version=2,
        capabilities=["recipe.install"],
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
        "build_input_sha256": "c" * 64,
        "dockerfile": "Dockerfile",
        "platform": "linux/arm64",
        "arguments": [],
        "network": {"mode": "none", "hosts": []},
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
                    "builder_agent_sha256": "f" * 64,
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
    claim = jobs.claim(
        NODE_A,
        "serial-a",
        30,
        protocol_version=2,
        capabilities=["recipe.build.v1"],
        runtime_identity={
            "active_slot": "B",
            "architecture": "linux-arm64",
            "agent_sha256": "e" * 64,
            "build_digest": "sha256:" + "d" * 64,
            "platform_version": "1.2.3",
            "self_test_passed": True,
            "supervisor_generation": 2,
            "supervisor_ready_generation": 2,
        },
    )

    assert claim is None
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        parent_row = session.get(Job, parent_job.id)
        node = session.get(AgentNode, NODE_A)
        build = session.get(RecipeBuild, build_id)
        reservation = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == build_id
            )
        )
        assert stored is not None and stored.state == "failed"
        assert parent_row is not None and parent_row.state == "failed"
        assert node is not None and node.agent_sha256 == "e" * 64
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
        "build_input_sha256": "c" * 64,
        "dockerfile": "Dockerfile",
        "platform": "linux/arm64",
        "arguments": [],
        "network": {"mode": "none", "hosts": []},
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
                    "builder_agent_sha256": "f" * 64,
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

    claim = jobs.claim(
        NODE_A,
        "serial-a",
        30,
        protocol_version=2,
        capabilities=["recipe.build.v1"],
    )

    assert claim is None
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state == "failed"


def test_update_enqueue_persists_one_signed_payload_and_claims_its_reserved_fence(
    service,
) -> None:
    _, sessions, clock = service
    authority = UpdateAuthority()
    jobs = AgentJobService(sessions, clock=clock, update_authorizer=authority)
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.active_slot = "A"
        node.agent_sha256 = "f" * 64

    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        "agent.update",
        COMMIT,
        update_payload(),
        platform_target_name=PLATFORM_TARGET,
    )
    claim = jobs.claim(NODE_A, "serial-a", 30)

    assert claim is not None
    assert claim.fence == claim.payload["receipt"]["fence"]
    assert claim.operation_id == claim.payload["receipt"]["operation_id"]
    assert claim.payload_digest == hashlib.sha256(
        canonical_message(claim.payload)
    ).hexdigest()
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        assert stored.payload == claim.payload
        assert stored.payload_digest == claim.payload_digest
        assert session.scalars(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        ).one().fence == claim.fence
    assert authority.calls[0]["previous_slot"] == "A"
    assert authority.calls[0]["previous_sha256"] == "f" * 64


@pytest.mark.parametrize("fault", ("operation", "fence", "expired", "source"))
def test_stale_or_mismatched_update_receipt_never_reaches_an_agent_claim(
    service, fault: str
) -> None:
    _, sessions, clock = service
    jobs = AgentJobService(
        sessions, clock=clock, update_authorizer=UpdateAuthority()
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.active_slot = "A"
        node.agent_sha256 = "f" * 64
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        "agent.update",
        COMMIT,
        update_payload(),
        platform_target_name=PLATFORM_TARGET,
    )
    if fault == "expired":
        clock.advance(seconds=601)
    else:
        with sessions.begin() as session:
            stored = session.get(AgentOperation, operation.id)
            node = session.get(AgentNode, NODE_A)
            assert stored is not None and node is not None
            if fault == "source":
                node.agent_sha256 = "0" * 64
            else:
                receipt = stored.payload["receipt"]
                receipt[fault + "_id" if fault == "operation" else "fence"] = str(
                    uuid.uuid4()
                )
                stored.payload_digest = hashlib.sha256(
                    canonical_message(stored.payload)
                ).hexdigest()

    assert jobs.claim(NODE_A, "serial-a", 30) is None
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state == "waiting-for-operator"
        assert session.scalars(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        ).all() == []


def test_update_receipt_is_one_use_even_after_manual_retry_disposition(service) -> None:
    _, sessions, clock = service
    jobs = AgentJobService(
        sessions, clock=clock, update_authorizer=UpdateAuthority()
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.active_slot = "A"
        node.agent_sha256 = "f" * 64
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        "agent.update",
        COMMIT,
        update_payload(),
        platform_target_name=PLATFORM_TARGET,
    )
    first = jobs.claim(NODE_A, "serial-a", 1)
    assert first is not None
    clock.advance(seconds=2)
    assert jobs.claim(NODE_A, "serial-a", 30) is None
    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        stored.retry_disposition = "retry"
        stored.retry_disposition_attempt = 1

    assert jobs.claim(NODE_A, "serial-a", 30) is None
    with sessions() as session:
        attempts = session.scalars(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        ).all()
        assert len(attempts) == 1
        assert attempts[0].fence == first.fence


def test_update_signing_failure_rolls_back_enqueue_and_non_updates_are_not_signed(
    service,
) -> None:
    _, sessions, clock = service
    authority = UpdateAuthority(fail=True)
    jobs = AgentJobService(sessions, clock=clock, update_authorizer=authority)
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.active_slot = "A"
        node.agent_sha256 = "f" * 64
    parent_job = parent(sessions, clock)

    with pytest.raises(RuntimeError, match="signing unavailable"):
        jobs.enqueue(
            parent_job.id,
            NODE_A,
            "agent.update",
            COMMIT,
            update_payload(),
            platform_target_name=PLATFORM_TARGET,
        )
    probe = jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})

    with sessions() as session:
        assert session.get(AgentOperation, probe.id) is not None
        assert session.scalar(
            select(AgentOperation.id).where(
                AgentOperation.kind == "agent.update"
            )
        ) is None
    assert authority.calls == []


def test_node_cannot_claim_overlapping_platform_mutations(service) -> None:
    _, sessions, clock = service
    jobs = AgentJobService(
        sessions, clock=clock, update_authorizer=UpdateAuthority()
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.active_slot = "A"
        node.agent_sha256 = "f" * 64
    parent_job = parent(sessions, clock)
    update = jobs.enqueue(
        parent_job.id,
        NODE_A,
        "agent.update",
        COMMIT,
        update_payload(),
        platform_target_name=PLATFORM_TARGET,
    )
    rollback_parent = parent(sessions, clock)
    with sessions.begin() as session:
        stored_parent = session.get(Job, rollback_parent.id)
        assert stored_parent is not None
        stored_parent.kind = "platform.update"
        stored_parent.state = "waiting-for-operator"
    rollback = jobs.enqueue(
        rollback_parent.id, NODE_A, "agent.rollback", COMMIT, {}
    )

    first = jobs.claim(NODE_A, "serial-a", 30)

    assert first is not None
    assert first.operation_id in {update.id, rollback.id}
    assert jobs.claim(NODE_A, "serial-a", 30) is None


def test_signed_rollback_can_enqueue_beneath_waiting_platform_update_only(
    service,
) -> None:
    _, sessions, clock = service
    jobs = AgentJobService(
        sessions, clock=clock, update_authorizer=UpdateAuthority()
    )
    platform = parent(sessions, clock)
    unrelated = parent(sessions, clock)
    running = parent(sessions, clock)
    with sessions.begin() as session:
        platform_job = session.get(Job, platform.id)
        unrelated_job = session.get(Job, unrelated.id)
        running_job = session.get(Job, running.id)
        assert platform_job is not None and unrelated_job is not None
        assert running_job is not None
        platform_job.kind = "platform.update"
        platform_job.state = "waiting-for-operator"
        unrelated_job.state = "waiting-for-operator"

    rollback = jobs.enqueue(
        platform.id, NODE_A, "agent.rollback", COMMIT, {}
    )

    assert rollback.payload["receipt"]["action"] == "operator-rollback"
    with pytest.raises(ValueError, match="platform update"):
        jobs.enqueue(unrelated.id, NODE_B, "agent.rollback", COMMIT, {})
    with pytest.raises(ValueError, match="platform update"):
        jobs.enqueue(running.id, NODE_B, "agent.rollback", COMMIT, {})


def test_running_unrelated_parent_cannot_claim_injected_signed_rollback(
    service,
) -> None:
    _, sessions, clock = service
    authority = UpdateAuthority()
    jobs = AgentJobService(sessions, clock=clock, update_authorizer=authority)
    running = parent(sessions, clock)
    operation_id = str(uuid.uuid4())
    fence = str(uuid.uuid4())
    deadline = int(clock.now.timestamp()) + 600
    payload = authority.authorize_rollback(
        operation_id=operation_id,
        fence=fence,
        expires_at=deadline,
        current_slot="A",
        current_sha256="f" * 64,
        current_generation=1,
        node_id=NODE_A,
        attempt=1,
        claim_deadline=deadline,
        now=clock.now,
    )
    with sessions.begin() as session:
        session.add(
            AgentOperation(
                id=operation_id,
                parent_job_id=running.id,
                node_id=NODE_A,
                kind="agent.rollback",
                payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
                payload=payload,
                base_commit=COMMIT,
                state="queued",
                current_attempt=0,
                created_at=clock.now,
                updated_at=clock.now,
            )
        )

    assert jobs.claim(NODE_A, "serial-a", 30) is None
    with sessions() as session:
        operation = session.get(AgentOperation, operation_id)
        assert operation is not None
        assert operation.state == "waiting-for-operator"
        assert operation.current_attempt == 0


def test_invalid_rollback_is_quarantined_without_starving_valid_work(service) -> None:
    _, sessions, clock = service
    authority = UpdateAuthority()
    jobs = AgentJobService(sessions, clock=clock, update_authorizer=authority)
    invalid_parent = parent(sessions, clock)
    invalid_id = str(uuid.uuid4())
    fence = str(uuid.uuid4())
    deadline = int(clock.now.timestamp()) + 600
    payload = authority.authorize_rollback(
        operation_id=invalid_id,
        fence=fence,
        expires_at=deadline,
        current_slot="A",
        current_sha256="f" * 64,
        current_generation=1,
        node_id=NODE_A,
        attempt=1,
        claim_deadline=deadline,
        now=clock.now,
    )
    with sessions.begin() as session:
        session.add(
            AgentOperation(
                id=invalid_id,
                parent_job_id=invalid_parent.id,
                node_id=NODE_A,
                kind="agent.rollback",
                payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
                payload=payload,
                base_commit=COMMIT,
                state="queued",
                current_attempt=0,
                created_at=clock.now - timedelta(seconds=1),
                updated_at=clock.now,
            )
        )
    valid = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        "agent.update",
        COMMIT,
        update_payload(),
        platform_target_name=PLATFORM_TARGET,
    )

    claim = jobs.claim(NODE_A, "serial-a", 30)

    assert claim is not None and claim.operation_id == valid.id
    with sessions() as session:
        invalid = session.get(AgentOperation, invalid_id)
        assert invalid is not None and invalid.state == "waiting-for-operator"


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
    claim = jobs.claim(NODE_A, "serial-a", 30)
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
    operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})

    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: jobs.claim(NODE_A, "serial-a", 30), range(4)))

    claimed = [claim for claim in claims if claim is not None]
    assert len(claimed) == 1
    assert claimed[0].operation_id == operation.id


def test_long_poll_wakes_on_enqueue_and_times_out_without_per_client_state(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(jobs.claim, NODE_A, "serial-a", 30, 1.0)
        time.sleep(0.05)
        operation = jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
        claim = waiting.result(timeout=1)
    elapsed = time.monotonic() - started

    assert claim is not None and claim.operation_id == operation.id
    assert elapsed < 0.8

    timeout_started = time.monotonic()
    assert jobs.claim(NODE_B, "serial-b", 30, 0.08) is None
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
        waiting = pool.submit(jobs.claim, NODE_A, "serial-a", 30, 2.0)
        assert first_poll.wait(timeout=1)
        operation = other_process.enqueue(
            parent_job.id, NODE_A, "node.probe", COMMIT, {}
        )
        claim = waiting.result(timeout=0.8)

    assert claim is not None and claim.operation_id == operation.id


def test_expired_attempt_cannot_publish_success(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    first = jobs.claim(NODE_A, "serial-a", 30)
    assert first is not None

    clock.advance(seconds=31)
    second = jobs.claim(NODE_A, "serial-a", 30)
    assert second is not None

    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(first, PROBE_RESULT)
    jobs.succeed(second, PROBE_RESULT)


def test_revoked_expired_or_node_mismatched_certificate_cannot_claim(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    with sessions.begin() as session:
        session.get(AgentCertificate, "serial-a").revoked_at = clock.now  # type: ignore[union-attr]

    assert jobs.claim(NODE_A, "serial-a", 30) is None
    assert jobs.claim(NODE_A, "serial-b", 30) is None

    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.revoked_at = None
        certificate.not_after = clock.now

    assert jobs.claim(NODE_A, "serial-a", 30) is None


def test_enqueue_rejects_noncanonical_protocol_payload(service) -> None:
    jobs, sessions, clock = service

    with pytest.raises(ValueError, match="unsafe|protocol"):
        jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {"command": "uname"})
    with pytest.raises(ValueError, match="large|protocol"):
        jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {"value": "x" * 70_000})


@pytest.mark.parametrize("terminal_state", ("succeeded", "failed", "waiting-for-operator", "expired"))
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

    with pytest.raises(ValueError, match="base commit"):
        jobs.enqueue(parent_job.id, NODE_A, "node.probe", "b" * 40, {})
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
    claim = jobs.claim(NODE_A, "serial-a", 30)
    assert claim is not None

    progress = jobs.heartbeat(claim, {"phase": "checking"}, 60)

    assert progress.deadline > claim.deadline
    assert dict(progress.progress) == {"phase": "checking"}


def test_heartbeat_never_shortens_a_longer_existing_lease(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    claim = jobs.claim(NODE_A, "serial-a", 120)
    assert claim is not None
    clock.advance(seconds=10)

    progress = jobs.heartbeat(claim, {"phase": "checking"}, 30)

    assert progress.deadline >= claim.deadline


def test_update_heartbeat_is_capped_by_signed_receipt_expiry(service) -> None:
    _, sessions, clock = service
    jobs = AgentJobService(
        sessions, clock=clock, update_authorizer=UpdateAuthority()
    )
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        "agent.update",
        COMMIT,
        update_payload(),
        platform_target_name=PLATFORM_TARGET,
    )
    claim = jobs.claim(NODE_A, "serial-a", 30)
    assert claim is not None
    receipt_deadline = datetime.fromtimestamp(
        operation.payload["receipt"]["claim_deadline"], tz=UTC
    )
    assert claim.deadline == receipt_deadline
    clock.advance(seconds=500)

    progress = jobs.heartbeat(claim, {"phase": "activating"}, 300)

    assert progress.deadline == receipt_deadline
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == 1,
            )
        )
        assert attempt is not None
        persisted = attempt.lease_deadline
        if persisted.tzinfo is None:
            persisted = persisted.replace(tzinfo=UTC)
        assert persisted == receipt_deadline


def test_claim_persists_authenticated_running_release_identity(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    runtime_identity = {
        "active_slot": "B",
        "architecture": "linux-arm64",
        "agent_sha256": "c" * 64,
        "build_digest": "sha256:" + "b" * 64,
        "platform_version": "1.2.3",
        "self_test_passed": True,
        "supervisor_generation": 7,
        "supervisor_ready_generation": 7,
    }

    assert jobs.claim(
        NODE_A,
        "serial-a",
        30,
        protocol_version=1,
        runtime_identity=runtime_identity,
    ) is not None

    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert {
            "active_slot": node.active_slot,
            "architecture": node.architecture,
            "agent_sha256": node.agent_sha256,
            "build_digest": node.build_digest,
            "platform_version": node.platform_version,
            "self_test_passed": node.self_test_passed,
            "supervisor_generation": node.supervisor_generation,
            "supervisor_ready_generation": node.supervisor_ready_generation,
        } == runtime_identity


@pytest.mark.parametrize("architecture", ("linux-riscv64", True, 7))
def test_claim_rejects_malformed_runtime_architecture_without_persisting_it(
    service, architecture: object
) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    runtime_identity = {
        "active_slot": "B",
        "architecture": architecture,
        "agent_sha256": "c" * 64,
        "build_digest": "sha256:" + "b" * 64,
        "platform_version": "1.2.3",
        "self_test_passed": True,
        "supervisor_generation": 7,
        "supervisor_ready_generation": 7,
    }

    with pytest.raises(ValueError, match="runtime identity"):
        jobs.claim(
            NODE_A,
            "serial-a",
            30,
            protocol_version=1,
            runtime_identity=runtime_identity,
        )

    with sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.architecture is None


@pytest.mark.parametrize("agent_action", ("heartbeat", "result"))
def test_retired_identity_cannot_mutate_active_attempt_or_record_contact(
    service, agent_action: str
) -> None:
    jobs, sessions, clock = service
    operation = jobs.enqueue(
        parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {}
    )
    claim = jobs.claim(NODE_A, "serial-a", 30, protocol_version=2)
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
        attempt = session.scalar(select(AgentOperationAttempt).where(
            AgentOperationAttempt.operation_id == operation.id,
            AgentOperationAttempt.attempt == claim.attempt,
        ))
        assert node is not None and node.last_seen_at is None
        assert node.protocol_version == 2
        assert stored_operation is not None and stored_operation.state == "running"
        assert attempt is not None and attempt.state == "running"
        assert attempt.progress is None and attempt.result is None


def test_public_fence_string_interface_renews_and_completes(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    claim = jobs.claim(NODE_A, "serial-a", 30)
    assert claim is not None

    progress = jobs.heartbeat(claim.fence, {"phase": "checking"}, 60)
    jobs.succeed(progress.fence, PROBE_RESULT)

    with pytest.raises(StaleAgentAttempt):
        jobs.fail(str(uuid.uuid4()), "unknown fence")


def test_structured_fence_cannot_update_a_different_operation(service) -> None:
    jobs, sessions, clock = service
    first_operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    second_operation = jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    first = jobs.claim(NODE_A, "serial-a", 30)
    assert first is not None
    other_operation = second_operation if first.operation_id == first_operation.id else first_operation
    forged = type(first)(**{**first.__dict__, "operation_id": other_operation.id})
    with pytest.raises(StaleAgentAttempt):
        jobs.heartbeat(forged, {"phase": "forged"}, 30)
    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(forged, PROBE_RESULT)
    assert first.operation_id != other_operation.id


def test_attempt_expiring_exactly_at_claim_time_is_reclaimable(service) -> None:
    jobs, sessions, clock = service
    jobs.enqueue(parent(sessions, clock).id, NODE_A, "node.probe", COMMIT, {})
    first = jobs.claim(NODE_A, "serial-a", 30)
    assert first is not None

    clock.advance(seconds=30)
    second = jobs.claim(NODE_A, "serial-a", 30)

    assert second is not None
    assert second.fence != first.fence


def test_parent_job_becomes_succeeded_only_after_every_operation_succeeds(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
    jobs.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})

    first = jobs.claim(NODE_A, "serial-a", 30)
    assert first is not None
    jobs.succeed(first, PROBE_RESULT)
    assert job_state(sessions, parent_job.id).state == "queued"

    second = jobs.claim(NODE_B, "serial-b", 30)
    assert second is not None
    jobs.succeed(second, PROBE_RESULT)

    assert job_state(sessions, parent_job.id).state == "succeeded"


def test_parent_job_fails_when_all_operations_are_terminal_and_one_failed(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
    jobs.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})

    failed = jobs.claim(NODE_A, "serial-a", 30)
    assert failed is not None
    jobs.fail(failed, "token=sensitive " + "x" * 2_000)
    assert job_state(sessions, parent_job.id).state == "queued"

    succeeded = jobs.claim(NODE_B, "serial-b", 30)
    assert succeeded is not None
    jobs.succeed(succeeded, PROBE_RESULT)

    aggregate = job_state(sessions, parent_job.id)
    assert aggregate.state == "failed"
    assert aggregate.status_reason is not None
    assert "sensitive" not in aggregate.status_reason
    assert len(aggregate.status_reason) <= 1024


def test_parent_job_waits_when_all_operations_terminal_without_failures(service) -> None:
    jobs, sessions, clock = service
    parent_job = parent(sessions, clock)
    jobs.enqueue(parent_job.id, NODE_A, "node.probe", COMMIT, {})
    jobs.enqueue(parent_job.id, NODE_B, "node.probe", COMMIT, {})

    waiting = jobs.claim(NODE_A, "serial-a", 30)
    assert waiting is not None
    jobs.wait_for_operator(waiting, "confirm displayed fingerprint")

    succeeded = jobs.claim(NODE_B, "serial-b", 30)
    assert succeeded is not None
    jobs.succeed(succeeded, PROBE_RESULT)

    assert job_state(sessions, parent_job.id).state == "waiting-for-operator"
