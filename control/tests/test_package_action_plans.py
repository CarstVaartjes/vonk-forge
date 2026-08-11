from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from vonk_control.agent_jobs import AgentJobService
from vonk_control.db import build_engine, session_factory
from vonk_control.models import (
    AgentNode,
    Base,
    Observation,
    PackageActionPlan,
    PackageCandidate,
    PackageObservation,
    PackageResolution,
    PackageValidationRun,
)
from vonk_control.models import AgentOperation as StoredAgentOperation
from vonk_control.package_services import ProductionPackageProjectionService
from vonk_control.repository import RepositoryService


def test_package_action_plan_persists_bounded_digest_bound_request(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    now = datetime.now(UTC)
    digest = "a" * 64
    with sessions.begin() as session:
        session.add(
            PackageActionPlan(
                plan_digest=digest,
                action="package.remove",
                subject="synthetic-canary",
                request={"release_digest": "b" * 64, "node_ids": ["spk_" + "1" * 32]},
                state="planned",
                expires_at=now + timedelta(minutes=15),
                created_at=now,
                updated_at=now,
            )
        )
    with sessions() as session:
        stored = session.get(PackageActionPlan, digest)
        assert stored is not None
        assert stored.request["node_ids"] == ["spk_" + "1" * 32]


def test_package_action_plan_rejects_unknown_action(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    now = datetime.now(UTC)
    with pytest.raises(IntegrityError), sessions.begin() as session:
        session.add(
            PackageActionPlan(
                plan_digest="a" * 64,
                action="shell.exec",
                subject="x",
                request={"x": 1},
                state="planned",
                expires_at=now,
                created_at=now,
                updated_at=now,
            )
        )


def test_projection_service_reuses_exact_preview_and_rejects_changed_apply(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)

    class Repository:
        def head(self):
            return "a" * 40

    service = ProductionPackageProjectionService(Repository(), sessions)
    first = service.create_action_plan(
        "package.remove", "synthetic-canary", {"node_ids": ["spk_" + "1" * 32]}
    )
    second = service.create_action_plan(
        "package.remove", "synthetic-canary", {"node_ids": ["spk_" + "1" * 32]}
    )
    assert first == second
    request = service.consume_action_plan(first, "package.remove", "synthetic-canary")
    assert request["node_ids"] == ["spk_" + "1" * 32]
    with pytest.raises(ValueError, match="plan action or subject"):
        service.consume_action_plan(first, "package.gc", "synthetic-canary")


def test_removal_apply_queues_typed_worker_operations_and_replays(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    queued: list[tuple[str, str, dict[str, object]]] = []

    class Jobs:
        def enqueue_in_session(self, session, job_id, node_id, operation, base_commit, payload, *, operation_id):
            del session, job_id, base_commit, operation_id
            queued.append((node_id, operation, dict(payload)))

        def notify_available(self):
            return None

    root = RepositoryService(Path(__file__).resolve().parents[2])
    service = ProductionPackageProjectionService(root, sessions, agent_jobs=Jobs())
    preview = service.removal_preview(
        "ds4-deepseek-single",
        "sha256:" + "a" * 64,
        ("spk_" + "1" * 32,),
    )
    result = service.remove(preview["digest"], "admin", "request-1")
    assert result["state"] == "planned"
    assert queued[0][0] == "spk_" + "1" * 32
    assert queued[0][1] == "package.remove"
    assert queued[0][2]["release_digest"] == "a" * 64
    assert service.remove(preview["digest"], "admin", "request-1") == result


def test_gc_preview_and_apply_fan_out_per_node_without_stopping_workloads(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    node_id = "spk_" + "2" * 32
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.add(
            Observation(
                node_id=node_id,
                kind="health",
                payload={"storage": {"total_bytes": 1000, "free_bytes": 100, "reclaimable_bytes": 400}},
                observed_at=now,
            )
        )
    queued: list[tuple[str, str, dict[str, object]]] = []

    class Jobs:
        def enqueue_in_session(self, session, job_id, node_id, operation, base_commit, payload, *, operation_id):
            del session, job_id, base_commit, operation_id
            queued.append((node_id, operation, dict(payload)))

        def notify_available(self):
            return None

    root = RepositoryService(Path(__file__).resolve().parents[2])
    service = ProductionPackageProjectionService(
        root, sessions, agent_jobs=Jobs()
    )
    preview = service.gc_preview()
    assert preview["reclaim_bytes"] == 400
    result = service.gc(preview["digest"], "admin", "request-gc")
    assert result["state"] == "planned"
    assert queued == [(node_id, "package.gc", {"schema_version": 1, "dry_run": False, "target_bytes": 400})]


def test_removal_uses_real_agent_job_boundary(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    node_id = "spk_" + "3" * 32
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
    jobs = AgentJobService(sessions, clock=lambda: now)
    service = ProductionPackageProjectionService(
        RepositoryService(Path(__file__).resolve().parents[2]), sessions, agent_jobs=jobs
    )
    preview = service.removal_preview(
        "ds4-deepseek-single", "sha256:" + "c" * 64, (node_id,)
    )
    result = service.remove(preview["digest"], "admin", "request-real")
    assert result["state"] == "planned"
    with sessions() as session:
        operation = session.scalar(select(StoredAgentOperation))
        assert operation is not None
        assert operation.kind == "package.remove"
        assert operation.node_id == node_id


def test_rollout_preview_and_apply_use_digest_plan_and_existing_orchestrator(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    node_id = "spk_" + "4" * 32
    queued: list[tuple[str, str, dict[str, object]]] = []

    class Jobs:
        def enqueue_in_session(self, session, job_id, node_id, operation, base_commit, payload, *, operation_id):
            del session, job_id, base_commit, operation_id
            queued.append((node_id, operation, dict(payload)))
            return type("Stored", (), {"id": "operation-id"})()

        def notify_available(self):
            return None

    root = RepositoryService(Path(__file__).resolve().parents[2])
    service = ProductionPackageProjectionService(
        root,
        sessions,
        fleet=lambda: {
            "nodes": [
                {
                    "id": node_id,
                    "healthy": True,
                    "agent_state": "active",
                    "agent_online": True,
                    "memory_available_bytes": 2_000_000_000_000,
                    "disk_available_bytes": 2_000_000_000_000,
                    "gpu_memory_available_bytes": 2_000_000_000_000,
                    "labels": {},
                    "capabilities": ["package-abi-v1"],
                    "architecture": "arm64",
                    "operating_system": "linux",
                }
            ]
        },
        agent_jobs=Jobs(),
        package_trust=lambda _release, _lock, _commit: True,
    )
    preview = service.rollout_preview("ds4-deepseek-single")
    assert preview["state"] == "ready"
    assert preview["download_bytes"] == 93_691_352_994
    assert preview["storage_bytes"] == 213_691_352_994
    assert preview["resource_envelope"]["required_nodes"] == 1
    assert preview["resource_envelope"]["per_node"]["resident_memory_bytes"] > 0
    assert preview["resource_envelope"]["world_size"] == 1
    assert preview["resource_envelope"]["fabric"]["kind"] == "none"
    result = service.rollout(
        "ds4-deepseek-single", preview["digest"], "admin", "request-rollout"
    )
    assert result["state"] in {"planned", "running"}
    assert any(item[1] == "package.prepare" for item in queued)


def test_repair_preview_and_apply_queues_package_repair(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    node_id = "spk_" + "5" * 32
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.add(
            Observation(
                node_id=node_id,
                kind="health",
                payload={"status": "healthy", "storage": {"total_bytes": 1000, "free_bytes": 900}},
                observed_at=now,
            )
        )
    queued: list[tuple[str, str, dict[str, object]]] = []

    class Jobs:
        def enqueue_in_session(self, session, job_id, node_id, operation, base_commit, payload, *, operation_id):
            del session, job_id, base_commit, operation_id
            queued.append((node_id, operation, dict(payload)))

        def notify_available(self):
            return None

    service = ProductionPackageProjectionService(
        RepositoryService(Path(__file__).resolve().parents[2]), sessions, agent_jobs=Jobs()
    )
    preview = service.repair_preview("ds4-deepseek-single")
    result = service.repair("ds4-deepseek-single", preview["digest"], "admin", "request-repair")
    assert result["state"] == "planned"
    assert queued[0][1] == "package.repair"


def test_inventory_projects_resource_envelope_from_signed_release_not_agent_summary(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'inventory.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    node_id = "spk_" + "7" * 32
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.add(
            PackageObservation(
                node_id=node_id,
                deployment_id="ds4-deepseek-single",
                release_digest="c11e4bce2c20e8047666d5d1c4c87dac164d01f7a5ffcf92452321cb53d65a45",
                observation_digest="f" * 64,
                state="active",
                summary={
                    "family_id": "ds4-deepseek",
                    "resources": {
                        "download_bytes": 0,
                        "installed_bytes": 0,
                        "transient_bytes": 0,
                        "output_bytes": 0,
                        "host_memory_bytes": 0,
                        "gpu_memory_bytes": 0,
                        "kv_cache_base_bytes": 0,
                        "kv_cache_per_token_bytes": 0,
                        "required_nodes": 1,
                        "topology": "single",
                    },
                },
                observed_at=now,
            )
        )
    service = ProductionPackageProjectionService(
        RepositoryService(Path(__file__).resolve().parents[2]), sessions
    )

    package = service.inventory(node_id, None, None, 10)["nodes"][0]["packages"][0]

    assert package["resources"]["download_bytes"] == 93_691_352_994
    assert package["resources"]["host_memory_bytes"] == 120_000_000_000


def test_promotion_preview_and_apply_are_fenced_to_publication_service(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    candidate_id = "00000000-0000-4000-8000-000000000006"
    release_digest = "d" * 64
    with sessions.begin() as session:
        session.add(
            PackageCandidate(
                id=candidate_id,
                family_id="future-stack",
                upstream_identity_digest="a" * 64,
                metadata_digest="b" * 64,
                upstream_version="1.0.0",
                channel="stable",
                source_provider="git",
                source_reference="future-stack/1.0.0",
                state="resolved",
                summary={"release": {"release_digest": release_digest}},
                discovered_by="test",
                first_seen_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    class Repository:
        def head(self):
            return "a" * 40

    published: list[tuple[str, str]] = []

    class Publication:
        def preview(self, candidate: str, commit: str):
            assert candidate == candidate_id
            return type(
                "Preview",
                (),
                {"digest": "e" * 64, "release_digest": release_digest, "base_commit": commit},
            )()

        def promote(self, preview_digest: str, actor: str):
            published.append((preview_digest, actor))
            return type("Target", (), {"digest": release_digest})()

    service = ProductionPackageProjectionService(
        Repository(), sessions, publication=Publication()
    )
    preview = service.promotion_preview(candidate_id)
    assert preview["candidate_id"] == candidate_id
    assert preview["release_digest"] == "sha256:" + release_digest
    result = service.promote(candidate_id, preview["digest"], "admin", "request-promote")
    assert result == {
        "candidate_id": candidate_id,
        "release_digest": "sha256:" + release_digest,
        "digest": "sha256:" + release_digest,
        "state": "published",
    }
    assert published == [("e" * 64, "admin")]
    assert service.promote(candidate_id, preview["digest"], "admin", "request-promote") == result


def test_publication_candidate_reads_lock_from_git_and_validation_from_sql(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'plans.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    root = RepositoryService(Path(__file__).resolve().parents[2])
    release_digest = "c11e4bce2c20e8047666d5d1c4c87dac164d01f7a5ffcf92452321cb53d65a45"
    candidate_id = "00000000-0000-4000-8000-000000000007"
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            PackageCandidate(
                id=candidate_id,
                family_id="ds4-deepseek",
                upstream_identity_digest="a" * 64,
                metadata_digest="b" * 64,
                upstream_version="2026.08.legacy-ds4",
                source_provider="git",
                source_reference="ds4",
                state="resolved",
                summary={
                    "evidence": {
                        "lock_digest": release_digest,
                        "provenance_digest": "b" * 64,
                        "sbom_digest": "c" * 64,
                        "schema_version": 1,
                    }
                },
                discovered_by="test",
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        resolution_id = "00000000-0000-4000-8000-000000000008"
        session.add(
            PackageResolution(
                id=resolution_id,
                candidate_id=candidate_id,
                resolver_id="test",
                resolver_schema_version=1,
                state="resolved",
                release_digest=release_digest,
                resolved_by="test",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            PackageValidationRun(
                id="00000000-0000-4000-8000-000000000009",
                candidate_id=candidate_id,
                resolution_id=resolution_id,
                validation_kind="artifact",
                release_digest=release_digest,
                policy_digest="d" * 64,
                fleet_digest="e" * 64,
                state="passed",
                actor="test",
                evidence={"artifact": "verified"},
                created_at=now,
                updated_at=now,
            )
        )
    service = ProductionPackageProjectionService(root, sessions)
    value = service.publication_candidate(candidate_id)
    assert value["release_digest"] == release_digest
    assert isinstance(value["lock_bytes"], bytes)
    assert value["evidence"]["provenance_digest"] == "b" * 64
    assert value["validation"]["state"] == "passed"


def test_validation_preview_persists_a_digest_bound_run(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'validation.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    candidate_id = "00000000-0000-4000-8000-000000000010"
    release_digest = "c11e4bce2c20e8047666d5d1c4c87dac164d01f7a5ffcf92452321cb53d65a45"
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            PackageCandidate(
                id=candidate_id,
                family_id="ds4-deepseek",
                upstream_identity_digest="a" * 64,
                metadata_digest="b" * 64,
                upstream_version="2026.08.legacy-ds4",
                source_provider="git",
                source_reference="ds4",
                state="resolved",
                summary={
                    "evidence": {
                        "lock_digest": release_digest,
                        "provenance_digest": "b" * 64,
                        "sbom_digest": "c" * 64,
                    }
                },
                discovered_by="test",
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            PackageResolution(
                id="00000000-0000-4000-8000-000000000011",
                candidate_id=candidate_id,
                resolver_id="test",
                resolver_schema_version=1,
                state="resolved",
                release_digest=release_digest,
                resolved_by="test",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            AgentNode(
                node_id="spk_" + "1" * 32,
                state="active",
                capabilities=["package-abi-v1", "package-backend-oci-v1"],
                architecture="linux-arm64",
            )
        )
        session.add(
            Observation(
                node_id="spk_" + "1" * 32,
                kind="health",
                payload={
                    "status": "healthy",
                    "authenticated": True,
                    "online": True,
                    "architecture": "linux-arm64",
                    "operating_system": "linux",
                    "memory_available_bytes": 200_000_000_000,
                    "disk_available_bytes": 200_000_000_000,
                    "storage_available_bytes": 200_000_000_000,
                },
                observed_at=now,
            )
        )

    service = ProductionPackageProjectionService(
        RepositoryService(Path(__file__).resolve().parents[2]),
        sessions,
        fleet=lambda: {
            "nodes": [
                {
                    "node_id": "spk_" + "1" * 32,
                    "healthy": True,
                    "agent_online": True,
                    "authenticated": True,
                    "architecture": "linux-arm64",
                    "operating_system": "linux",
                    "memory_available_bytes": 200_000_000_000,
                    "disk_available_bytes": 200_000_000_000,
                    "storage_available_bytes": 200_000_000_000,
                    "capabilities": ["package-abi-v1", "package-backend-oci-v1"],
                }
            ]
        },
        validation_runner=lambda _request: {
            "status": "passed",
            "evidence": {
                "checksum": "a" * 64,
                "provenance": "b" * 64,
                "artifact": "c" * 64,
                "health": "d" * 64,
                "inference": "e" * 64,
            },
        },
    )

    preview = service.validation_preview(candidate_id)

    assert preview["candidate_id"] == candidate_id
    assert preview["release_digest"] == "sha256:" + release_digest
    assert preview["state"] == "ready"
    assert preview["digest"].startswith("sha256:")
    result = service.validate(
        candidate_id, preview["digest"], "admin", "request-validation"
    )
    assert result["state"] == "passed"
    with sessions() as session:
        run = session.get(PackageValidationRun, preview["validation_id"])
        assert run is not None
        assert run.state == "passed"
        assert run.release_digest == release_digest
        assert run.progress["total"] == 2


def test_rollback_preview_binds_retained_rollout_predecessor(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'rollback.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    node_id = "spk_" + "6" * 32
    queued: list[tuple[str, str, dict[str, object]]] = []

    class Jobs:
        def enqueue_in_session(self, session, job_id, node_id, operation, base_commit, payload, *, operation_id):
            del session, job_id, base_commit
            queued.append((node_id, operation, dict(payload)))
            return type("Stored", (), {"id": operation_id})()

        def notify_available(self):
            return None

    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                capabilities=["package-abi-v1"],
                architecture="linux-arm64",
            )
        )
        session.add(
            PackageObservation(
                node_id=node_id,
                deployment_id="ds4-deepseek-single",
                release_digest="c" * 64,
                observation_digest="e" * 64,
                state="active",
                summary={"deployment_digest": "d" * 64},
                observed_at=datetime.now(UTC),
            )
        )

    service = ProductionPackageProjectionService(
        RepositoryService(Path(__file__).resolve().parents[2]),
        sessions,
        fleet=lambda: {
            "nodes": [
                {
                    "id": node_id,
                    "healthy": True,
                    "agent_state": "active",
                    "agent_online": True,
                    "memory_available_bytes": 2_000_000_000_000,
                    "disk_available_bytes": 2_000_000_000_000,
                    "gpu_memory_available_bytes": 2_000_000_000_000,
                    "labels": {},
                    "capabilities": ["package-abi-v1"],
                    "architecture": "arm64",
                    "operating_system": "linux",
                    "current_packages": {
                        "ds4-deepseek-single": {
                            "release_digest": "c" * 64,
                            "deployment_digest": "d" * 64,
                        }
                    },
                }
            ]
        },
        agent_jobs=Jobs(),
        package_trust=lambda _release, _lock, _commit: True,
    )
    rollout_preview = service.rollout_preview("ds4-deepseek-single")
    rollout = service.rollout(
        "ds4-deepseek-single", rollout_preview["digest"], "admin", "request-rollout"
    )

    rollback_preview = service.rollback_preview(
        "ds4-deepseek-single", rollout["id"]
    )

    assert rollback_preview["state"] == "ready"
    assert rollback_preview["release_digest"] == "sha256:" + "c" * 64
    result = service.rollback(
        "ds4-deepseek-single",
        rollout["id"],
        rollback_preview["digest"],
        "admin",
        "request-rollback",
    )
    assert result["state"] == "planned"
    assert queued[-1][1] == "package.rollback"
    assert queued[-1][2]["release_digest"] == "c" * 64
    assert queued[-1][2]["deployment_digest"] == "d" * 64
