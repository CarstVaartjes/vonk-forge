from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentOperation, PackageReleaseLock
from vonk_control.agent_jobs import AgentJobService
from vonk_control.models import (
    AgentNode,
    Base,
    PackageRolloutNode,
)
from vonk_control.models import (
    AgentOperation as StoredAgentOperation,
)
from vonk_control.package_rollouts import (
    PackageDesiredStateResolver,
    PackageRolloutError,
    PackageRolloutOrchestrator,
    package_operation_payload,
)

from cluster_profiles.workload_packages import WorkloadDeployment

COMMIT = "a" * 40
NODE = "spk_" + "1" * 32


def _deployment(release: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "deployment_id": "future-stack",
        "family_id": "synthetic-family",
        "release_digest": release,
        "selector": {
            "node_count": 1,
            "required_labels": {"pool": "default"},
            "preferred_node_ids": [NODE],
        },
        "secrets": {},
        "ports": {"http": 8000},
        "arguments": [],
        "routing": {"alias": "chat", "port": "http"},
        "resources": {"memory_bytes": 1, "storage_bytes": 1, "gpu_count": 1},
    }


def _lock() -> PackageReleaseLock:
    component = {
        "name": "payload",
        "kind": "artifact",
        "media_type": "application/octet-stream",
        "sources": [{"provider": "https", "url": "https://example.invalid/a"}],
        "digest": "sha256:" + "1" * 64,
        "size": 1,
        "unpacked_size": 1,
        "platforms": ["linux/arm64"],
        "materialization": {"method": "file"},
        "evidence": [],
    }
    return PackageReleaseLock.parse(
        {
            "schema_version": 1,
            "family_id": "synthetic-family",
            "upstream_version": "1",
            "upstream_identity": {
                "provider": "git",
                "repository": "https://example.invalid/repo",
                "commit": "b" * 40,
            },
            "components": [component],
            "dependency_digests": [],
            "adapter": component
            | {
                "name": "adapter",
                "kind": "adapter",
                "materialization": {"method": "executable"},
            },
            "adapter_abi": 1,
            "compatibility": {
                "architectures": ["arm64"],
                "operating_systems": ["linux"],
                "required_capabilities": [],
                "minimum_storage_bytes": 1,
            },
            "validation": [],
            "provenance": [],
            "resolver": {"name": "resolver", "version": 1},
            "resource_envelope": {
                "schema_version": 1,
                "per_node": {
                    "download_bytes": 1,
                    "installed_bytes": 1,
                    "transient_bytes": 1,
                    "output_bytes": 0,
                    "host_memory_bytes": 1,
                    "resident_memory_bytes": 1,
                    "auxiliary_memory_bytes": 0,
                    "activation_memory_bytes": 0,
                    "workspace_memory_bytes": 0,
                    "gpu_memory_bytes": 1,
                    "gpu_count": 1,
                    "cpu_millicores": 1,
                    "kv_cache_base_bytes": 0,
                    "kv_cache_per_token_bytes": 0,
                },
                "aggregate": {
                    "download_bytes": 1,
                    "installed_bytes": 1,
                    "transient_bytes": 1,
                    "output_bytes": 0,
                    "host_memory_bytes": 1,
                    "resident_memory_bytes": 1,
                    "auxiliary_memory_bytes": 0,
                    "activation_memory_bytes": 0,
                    "workspace_memory_bytes": 0,
                    "gpu_memory_bytes": 1,
                    "gpu_count": 1,
                    "cpu_millicores": 1,
                    "kv_cache_base_bytes": 0,
                    "kv_cache_per_token_bytes": 0,
                },
                "required_nodes": 1,
                "topology": "single",
                "world_size": 1,
                "ranks": [{"rank": 0, "role": "primary"}],
                "fabric": {"kind": "none", "min_bandwidth_mbps": 0},
                "measurement": "declared",
                "evidence": [{"kind": "capacity", "digest": "sha256:" + "2" * 64}],
            },
        }
    )


@dataclass
class _Document:
    parsed: object
    content: bytes
    sha256: str


class _Repository:
    def __init__(
        self,
        deployment: dict[str, object],
        lock: PackageReleaseLock,
        raw_lock: bytes | None = None,
    ):
        self.deployment = deployment
        self.lock = lock
        self.raw_lock = raw_lock

    def read_document(self, commit: str, path: str) -> _Document:
        if path.startswith("config/workload-deployments/"):
            raw = json.dumps(self.deployment, sort_keys=True).encode()
        else:
            raw = self.raw_lock or self.lock.canonical_bytes
        parsed = json.loads(raw)
        return _Document(parsed, raw, hashlib.sha256(raw).hexdigest())


def test_package_payload_is_exact_digest_bound_protocol_message() -> None:
    release = "a" * 64
    deployment = WorkloadDeployment.load(_deployment(release))
    payload = package_operation_payload(deployment, "package.prepare")
    assert set(payload) == {
        "schema_version",
        "deployment_id",
        "release_digest",
        "deployment_digest",
        "deployment",
        "deployment_config_digest",
    }
    assert (
        payload["deployment_digest"]
        == hashlib.sha256(deployment.canonical_bytes).hexdigest()
    )
    assert payload["deployment"] == json.loads(deployment.canonical_bytes)
    assert payload["deployment_config_digest"] == payload["deployment_digest"]


def test_unknown_family_resolves_without_static_adapter_catalog() -> None:
    lock = _lock()
    deployment = _deployment(lock.digest)
    resolver = PackageDesiredStateResolver(
        _Repository(deployment, lock),
        trust=lambda digest, raw, commit: (
            digest == lock.digest and raw == lock.canonical_bytes
        ),
    )
    plan = resolver.resolve(
        COMMIT,
        ("future-stack",),
        (
            {
                "node_id": NODE,
                "healthy": True,
                "labels": {"pool": "default"},
                "memory_available_bytes": 4096,
                "disk_available_bytes": 4096,
                "gpu_memory_available_bytes": 4096,
            },
        ),
    )
    # Package operations require the v2 generic package ABI.  A v1 GPU node must
    # not be selected for a package graph and would otherwise be unable to
    # claim any of the queued package operations.
    assert plan.agent_protocol_range == (2, 2)
    kinds = {node.kind for node in plan.operation_graph.nodes}  # type: ignore[union-attr]
    assert {"package.prepare", "package.activate", "package.health"} <= kinds
    assert "agent.update" not in kinds
    assert all(
        payload["release_digest"] == lock.digest
        for payload in plan.operation_payloads.values()
    )


def test_release_without_resource_envelope_is_rejected_before_graph_creation() -> None:
    lock = _lock()
    legacy_document = json.loads(lock.canonical_bytes)
    legacy_document.pop("resource_envelope")
    raw_lock = json.dumps(legacy_document, sort_keys=True, separators=(",", ":")).encode()
    legacy_lock = PackageReleaseLock.parse(raw_lock)
    resolver = PackageDesiredStateResolver(
        _Repository(_deployment(hashlib.sha256(raw_lock).hexdigest()), legacy_lock, raw_lock),
        trust=lambda *_: True,
    )
    with pytest.raises(PackageRolloutError, match="resource envelope"):
        resolver.resolve(
            COMMIT,
            ("future-stack",),
            (
                {
                    "node_id": NODE,
                    "healthy": True,
                    "labels": {"pool": "default"},
                    "memory_available_bytes": 4096,
                    "disk_available_bytes": 4096,
                    "gpu_memory_available_bytes": 4096,
                },
            ),
        )


def test_resource_envelope_admission_rejects_insufficient_node_headroom() -> None:
    lock = _lock()
    envelope = dict(lock.resource_envelope or {})
    per_node = dict(envelope["per_node"])
    aggregate = dict(envelope["aggregate"])
    per_node["host_memory_bytes"] = 10_000
    aggregate["host_memory_bytes"] = 10_000
    rich_lock = replace(
        lock,
        resource_envelope={**envelope, "per_node": per_node, "aggregate": aggregate},
    )
    resolver = PackageDesiredStateResolver(
        _Repository(_deployment(rich_lock.digest), rich_lock), trust=lambda *_: True
    )

    with pytest.raises(PackageRolloutError, match="compatible node placement"):
        resolver.resolve(
            COMMIT,
            ("future-stack",),
            (
                {
                    "node_id": NODE,
                    "healthy": True,
                    "labels": {"pool": "default"},
                    "memory_available_bytes": 9_999,
                    "disk_available_bytes": 4096,
                    "gpu_memory_available_bytes": 4096,
                },
            ),
        )


def test_unsigned_release_is_rejected_before_graph_creation() -> None:
    lock = _lock()
    resolver = PackageDesiredStateResolver(
        _Repository(_deployment(lock.digest), lock), trust=lambda *_: False
    )
    with pytest.raises(PackageRolloutError, match="TUF-authorized"):
        resolver.resolve(
            COMMIT,
            ("future-stack",),
            ({"node_id": NODE, "healthy": True, "labels": {"pool": "default"}},),
        )


def test_replacement_stops_previous_digest_before_activation() -> None:
    lock = _lock()
    resolver = PackageDesiredStateResolver(
        _Repository(_deployment(lock.digest), lock), trust=lambda *_: True
    )
    plan = resolver.resolve(
        COMMIT,
        ("future-stack",),
        (
            {
                "node_id": NODE,
                "healthy": True,
                "labels": {"pool": "default"},
                "memory_available_bytes": 4096,
                "disk_available_bytes": 4096,
                "gpu_memory_available_bytes": 4096,
                "current_packages": {
                    "future-stack": {
                        "release_digest": "c" * 64,
                        "deployment_digest": "d" * 64,
                    }
                },
            },
        ),
    )
    operations = {node.kind: node for node in plan.operation_graph.nodes}  # type: ignore[union-attr]
    assert "package.stop" in operations
    assert operations["package.stop"].dependencies == (
        operations["package.prepare"].operation_id,
    )
    assert operations["package.activate"].dependencies == tuple(
        sorted(
            {
                operations["package.prepare"].operation_id,
                operations["package.stop"].operation_id,
            }
        )
    )
    release_projection = plan.releases["future-stack"]
    rollback_payload = release_projection["rollback_payloads"][NODE]  # type: ignore[index]
    assert rollback_payload["release_digest"] == "c" * 64
    assert rollback_payload["deployment_digest"] == "d" * 64


def test_failed_health_queues_fenced_predecessor_rollback(tmp_path) -> None:
    lock = _lock()
    resolver = PackageDesiredStateResolver(
        _Repository(_deployment(lock.digest), lock), trust=lambda *_: True
    )
    plan = resolver.resolve(
        COMMIT,
        ("future-stack",),
        (
            {
                "node_id": NODE,
                "healthy": True,
                "labels": {"pool": "default"},
                "memory_available_bytes": 4096,
                "disk_available_bytes": 4096,
                "gpu_memory_available_bytes": 4096,
                "current_packages": {
                    "future-stack": {
                        "release_digest": "c" * 64,
                        "deployment_digest": "d" * 64,
                    }
                },
            },
        ),
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'package-rollout.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = lambda: datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    queue = AgentJobService(sessions, clock=clock)
    orchestrator = PackageRolloutOrchestrator(sessions, queue, clock=clock)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE,
                state="active",
                capabilities=[
                    item.value
                    for item in AgentOperation
                    if item.value.startswith("package.")
                ],
            )
        )
    rollout_id = orchestrator.create(
        plan, "future-stack", actor="admin", request_id=str(uuid.uuid4())
    )
    for _ in range(3):
        assert orchestrator.advance(rollout_id) == "running"
        with sessions.begin() as session:
            node = session.scalar(
                select(PackageRolloutNode).where(
                    PackageRolloutNode.rollout_id == rollout_id
                )
            )
            assert node is not None and node.operation_id is not None
            operation = session.get(StoredAgentOperation, node.operation_id)
            assert operation is not None
            operation.state = "succeeded"
    assert orchestrator.advance(rollout_id) == "running"
    with sessions.begin() as session:
        node = session.scalar(
            select(PackageRolloutNode).where(
                PackageRolloutNode.rollout_id == rollout_id
            )
        )
        assert node is not None and node.operation_id is not None
        operation = session.get(StoredAgentOperation, node.operation_id)
        assert operation is not None
        operation.state = "failed"
    assert orchestrator.advance(rollout_id) == "rolling-back"
    with sessions() as session:
        node = session.scalar(
            select(PackageRolloutNode).where(
                PackageRolloutNode.rollout_id == rollout_id
            )
        )
        assert node is not None
        assert node.operation_kind == AgentOperation.PACKAGE_ROLLBACK.value
        assert node.state == "rolling-back"
        assert node.rollback_operation_id == node.operation_id
        assert node.operation_history[-1]["kind"] == AgentOperation.PACKAGE_ROLLBACK.value
