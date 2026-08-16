"""Acceptance contract for a workload family created after the agent build."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
for source_root in (ROOT / "agent/src", ROOT / "agent_protocol/src"):
    sys.path.insert(0, str(source_root))


FIXTURES = ROOT / "tests/fixtures/workload-packages/synthetic-upstream"
COMMIT = "a" * 40
NODE_ID = "spk_0123456789abcdef0123456789abcdef"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text())


def _component(document: dict[str, object]) -> tuple[dict[str, object], bytes]:
    component = document["component"]
    assert isinstance(component, dict)
    content = component["content"].encode("utf-8")
    source = component["source"]
    assert isinstance(source, dict)
    return (
        {
            "name": component["name"],
            "kind": component["kind"],
            "media_type": "application/octet-stream",
            "sources": [source],
            "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "unpacked_size": len(content),
            "platforms": ["linux/arm64"],
            "materialization": {"method": "file"},
            "evidence": [],
        },
        content,
    )


def _release_lock(name: str):
    from vonk_agent_protocol import PackageReleaseLock

    document = _fixture(name)
    component, content = _component(document)
    adapter_document = _fixture("adapter-abi-v1.json")
    adapter_content = adapter_document["content"].encode("utf-8")
    adapter = {
        "name": adapter_document["name"],
        "kind": "adapter",
        "media_type": "application/vnd.vonk-forge.workload-adapter.v1",
        "sources": [adapter_document["source"]],
        "digest": "sha256:" + hashlib.sha256(adapter_content).hexdigest(),
        "size": len(adapter_content),
        "unpacked_size": len(adapter_content),
        "platforms": ["linux/arm64"],
        "materialization": {"method": "executable"},
        "evidence": [],
    }
    lock = PackageReleaseLock.parse(
        {
            "schema_version": 1,
            "family_id": document["family_id"],
            "upstream_version": document["upstream_version"],
            "upstream_identity": {
                "provider": "git",
                "repository": "https://synthetic.invalid/unknown-stack.git",
                "commit": document["upstream_commit"],
            },
            "components": [component],
            "dependency_digests": [],
            "adapter": adapter,
            "adapter_abi": 1,
            "compatibility": {
                "architectures": ["arm64"],
                "operating_systems": ["linux"],
                "required_capabilities": ["package-abi-v1"],
                "minimum_storage_bytes": 1,
            },
            "validation": [],
            "provenance": [],
            "resolver": {"name": "synthetic-e2e", "version": 1},
            "resource_envelope": {
                "schema_version": 1,
                "per_node": {
                    "download_bytes": len(content) + len(adapter_content),
                    "installed_bytes": len(content) + len(adapter_content),
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
                    "download_bytes": len(content) + len(adapter_content),
                    "installed_bytes": len(content) + len(adapter_content),
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
                "evidence": [{"kind": "capacity", "digest": "sha256:" + "a" * 64}],
            },
        }
    )
    return lock, {component["digest"]: content, adapter["digest"]: adapter_content}


def _evidence(lock: object) -> dict[str, object]:
    return {
        "lock_digest": lock.digest,
        "provenance_digest": "b" * 64,
        "sbom_digest": "c" * 64,
        "schema_version": 1,
    }


def _candidate(lock: object) -> dict[str, object]:
    return {
        "family_id": lock.family_id,
        "release_digest": lock.digest,
        "release_lock_bytes": lock.canonical_bytes,
        "validation": {"state": "passed", "digest": "d" * 64},
        "evidence": _evidence(lock),
        "policy": {"mode": "manual"},
    }


class _SignedTargetSource:
    """Test-only bridge from the signed NAS repository to agent trust."""

    def __init__(self, delivery: object) -> None:
        self.delivery = delivery
        self.online = True
        self.refreshes = 0
        self.authorized: set[str] = set()

    def refresh(self) -> None:
        if not self.online:
            raise RuntimeError("simulated workload network is offline")
        self.refreshes += 1

    def trusted_target(self, name: str):
        from vonk_agent.package_trust import TrustedWorkloadTarget

        if not self.online:
            raise RuntimeError("simulated workload network is offline")
        prefix = "releases/"
        assert name.startswith(prefix) and name.endswith(".json")
        digest = name.removeprefix(prefix).removesuffix(".json")
        if digest not in self.authorized:
            raise RuntimeError("release is not present in signed workload TUF")
        data = self.delivery.target(digest)
        return TrustedWorkloadTarget(name, len(data), digest, data)


@dataclass(frozen=True)
class _FetchedObject:
    digest: str
    size: int
    kind: str
    path: str


class _DirectProvider:
    """Hermetic direct-provider boundary: no control relay or SSH is possible."""

    def __init__(self, contents: dict[str, bytes], events: list[str]) -> None:
        self.contents = contents
        self.events = events

    def fetch(self, descriptor, binding, progress, cancelled, *, deadline=None):
        del binding, deadline
        assert not cancelled()
        content = self.contents[descriptor.digest]
        assert hashlib.sha256(content).hexdigest() == descriptor.digest.removeprefix("sha256:")
        self.events.append(f"direct:{descriptor.sources[0]['provider']}:{descriptor.name}")
        progress({"phase": "direct-fetch", "component": descriptor.name})
        return _FetchedObject(
            descriptor.digest.removeprefix("sha256:"),
            len(content),
            descriptor.kind,
            f"objects/sha256/{descriptor.digest.removeprefix('sha256:')}",
        )


class _Materializer:
    def materialize(self, lock, objects, staging: Path):
        from vonk_agent.packages.materialize import MaterializedGeneration

        generation = staging / lock.digest
        generation.mkdir(parents=True, exist_ok=True)
        generation.chmod(0o555)
        return MaterializedGeneration(
            release_digest=lock.digest,
            root_object_digest="e" * 64,
            object_digests=tuple(sorted(objects)),
            environment_digest=None,
        )


class _Adapter:
    def __init__(self, lock: object, generation: str, events: list[str]) -> None:
        self.lock = lock
        self.generation = generation
        self.events = events

    def execute(self, operation, invocation, deadline):
        from vonk_agent.packages.adapter import AdapterEvidence

        del deadline
        self.events.append(f"adapter:{operation.value}")
        return AdapterEvidence(
            operation=operation,
            status="healthy" if operation.value == "health" else "ok",
            release_digest=self.lock.digest,
            generation=self.generation,
            fence=invocation.fence,
            evidence_digest=hashlib.sha256(
                f"{operation.value}:{self.lock.digest}:{self.generation}".encode()
            ).hexdigest(),
        )


class _UpdatesForbidden:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, *_args, **_kwargs):
        self.calls.append("agent.update")
        raise AssertionError("workload acceptance attempted agent.update")

    def rollback(self, *_args, **_kwargs):
        self.calls.append("agent.rollback")
        raise AssertionError("workload acceptance attempted agent.rollback")


def _claim(operation, payload: dict[str, object], index: int):
    from vonk_agent_protocol import AgentClaim, canonical_message

    return AgentClaim(
        schema_version=1,
        job_id=f"10000000-0000-4000-8000-{index:012d}",
        operation_id=f"20000000-0000-4000-8000-{index:012d}",
        attempt=1,
        fence=f"30000000-0000-4000-8000-{index:012d}",
        node_id=NODE_ID,
        operation=operation,
        base_commit=COMMIT,
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )


def test_unknown_family_simulator_delivers_signed_releases_without_agent_update_or_ssh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercise the generic package ABI for a family absent from agent build inputs."""
    from securesystemslib.signer import CryptoSigner
    from tuf.api.metadata import Metadata
    from vonk_agent.operations import OperationContext, OperationRegistry
    from vonk_agent.package_trust import WorkloadTrust
    from vonk_agent.packages.adapter import AdapterOperation
    from vonk_agent.packages.engine import PackageEngine
    from vonk_agent.packages.state import PackageState
    from vonk_agent.state import AgentStateStore
    from vonk_agent_protocol import AgentOperation
    from vonk_control.package_publication import PackagePublicationService
    from vonk_control.workload_trust import (
        WorkloadOnlineSigners,
        WorkloadTrustDelivery,
        WorkloadTrustError,
        WorkloadTrustPublisher,
        WorkloadTrustSigners,
        initialize_workload_trust,
    )

    agent_digest_before = hashlib.sha256(
        (ROOT / "agent/src/vonk_agent/main.py").read_bytes()
    ).hexdigest()
    ssh_calls: list[tuple[object, ...]] = []

    def forbid_ssh(*args, **kwargs):
        ssh_calls.append(tuple(args))
        raise AssertionError("workload acceptance attempted a subprocess/SSH path")

    monkeypatch.setattr(subprocess, "run", forbid_ssh)
    monkeypatch.setattr(subprocess, "Popen", forbid_ssh)

    release_one, bytes_one = _release_lock("release-native.json")
    release_two, bytes_two = _release_lock("release-oci.json")
    unapproved, _ = _release_lock("release-unapproved.json")
    assert release_one.family_id == release_two.family_id == "created-after-agent-build"
    assert release_one.adapter_abi == release_two.adapter_abi == 1

    signers = WorkloadTrustSigners(
        **{
            role: CryptoSigner.generate_ed25519()
            for role in ("root", "targets", "snapshot", "timestamp", "families", "releases")
        }
    )
    metadata_root = tmp_path / "nas/workload-tuf/metadata"
    target_root = tmp_path / "nas/workload-tuf/targets"
    now = datetime(2026, 8, 6, 12, tzinfo=UTC)
    initialize_workload_trust(
        metadata_root=metadata_root,
        target_root=target_root,
        signers=signers,
        now=now,
    )
    approved = {release_one.digest, release_two.digest}
    publisher = WorkloadTrustPublisher(
        metadata_root=metadata_root,
        target_root=target_root,
        signers=WorkloadOnlineSigners(
            releases=signers.releases,
            snapshot=signers.snapshot,
            timestamp=signers.timestamp,
        ),
        commit_eligible=lambda commit: commit == COMMIT,
        policy_authorized=lambda family, evidence: (
            family == "created-after-agent-build" and evidence["lock_digest"] in approved
        ),
        evidence_verified=lambda digest, evidence: evidence["lock_digest"] == digest,
        clock=lambda: now,
    )
    candidates = {
        "native": _candidate(release_one),
        "oci": _candidate(release_two),
        "unapproved": _candidate(unapproved),
    }
    publication = PackagePublicationService(
        candidates.__getitem__, head=lambda: COMMIT, publisher=publisher, clock=lambda: now
    )
    audit: list[dict[str, object]] = []
    delivery = WorkloadTrustDelivery(metadata_root=metadata_root, target_root=target_root)
    source = _SignedTargetSource(delivery)

    def promote(candidate_id: str) -> object:
        preview = publication.preview(candidate_id, COMMIT)
        trusted = publication.promote(preview.digest, "nas-admin@example")
        source.authorized.add(trusted.digest)
        audit.append(
            {
                "actor": "nas-admin@example",
                "candidate_id": candidate_id,
                "release_digest": trusted.digest,
                "tuf_snapshot_version": trusted.tuf_snapshot_version,
            }
        )
        return trusted

    target_one = promote("native")
    target_two = promote("oci")
    with pytest.raises(WorkloadTrustError, match="policy denied"):
        publication.promote(publication.preview("unapproved", COMMIT).digest, "nas-admin@example")
    assert not (target_root / unapproved.digest).exists()
    # The NAS promotion boundary rejects the unapproved candidate before it
    # can become a workload-TUF target; the GPU node therefore has no authorized
    # target for either an unsigned or an unapproved release digest.
    assert unapproved.digest not in source.authorized
    releases = Metadata.from_bytes(delivery.metadata("releases"))
    assert {
        f"releases/{target_one.digest}.json",
        f"releases/{target_two.digest}.json",
    } <= set(releases.signed.targets)

    events: list[str] = []
    progress: list[dict[str, object]] = []
    contents = bytes_one | bytes_two
    spark_root = tmp_path / "node"
    spark_root.mkdir(mode=0o700)
    state = PackageState(spark_root / "package-state")
    adapters: list[_Adapter] = []

    def adapter_factory(lock, generation, _path, _objects):
        adapter = _Adapter(lock, generation, events)
        adapters.append(adapter)
        return adapter

    engine = PackageEngine(
        state=state,
        trust=WorkloadTrust(source),
        acquisition=_DirectProvider(contents, events),
        materializer=_Materializer(),
        generation_root=tmp_path / "node/generations",
        pointer_root=tmp_path / "node/pointers",
        adapter_factory=adapter_factory,
        preflight=lambda lock, _request, _binding: events.append(f"preflight:{lock.family_id}"),
        progress=lambda _binding, value: progress.append(dict(value)),
        cancelled=lambda _binding: False,
    )
    update_guard = _UpdatesForbidden()
    context = OperationContext(
        node_id=NODE_ID,
        state=AgentStateStore(tmp_path / "node/agent-state"),
        probe=SimpleNamespace(collect=lambda _deadline: {}),
        updates=update_guard,
        packages=engine,
    )
    registry = OperationRegistry()

    def dispatch(operation, release, index: int) -> dict[str, object]:
        payload = {
            "schema_version": 1,
            "deployment_id": "unknown-after-build",
            "release_digest": release.digest,
            "deployment_digest": "f" * 64,
        }
        result = registry.execute(_claim(operation, payload, index), context)
        assert result["status"] == "ok"
        context.state.acknowledge(result.result)
        return dict(result["evidence"])

    assert dispatch(AgentOperation.PACKAGE_PREPARE, release_one, 1)["status"] == "validated"
    assert dispatch(AgentOperation.PACKAGE_ACTIVATE, release_one, 2)["status"] == "active"
    assert dispatch(AgentOperation.PACKAGE_HEALTH, release_one, 3)["status"] == "ok"
    assert dispatch(AgentOperation.PACKAGE_PREPARE, release_two, 4)["status"] == "validated"
    assert dispatch(AgentOperation.PACKAGE_ACTIVATE, release_two, 5)["status"] == "active"

    active_two = state.active_generation("unknown-after-build")
    assert active_two is not None and active_two.release_digest == release_two.digest
    from vonk_agent.packages.adapter import AdapterInvocation

    adapters[-1].execute(
        AdapterOperation.INFER,
            AdapterInvocation(
                job_id="10000000-0000-4000-8000-000000000006",
                operation_id="20000000-0000-4000-8000-000000000006",
                attempt=1,
                fence="30000000-0000-4000-8000-000000000006",
                release_digest=release_two.digest,
                generation=active_two.generation_id,
                node_id=NODE_ID,
            ),
        None,
    )

    refreshes_before_rollback = source.refreshes
    source.online = False
    assert dispatch(AgentOperation.PACKAGE_ROLLBACK, release_one, 7)["status"] == "active"
    assert source.refreshes == refreshes_before_rollback
    active_one = state.active_generation("unknown-after-build")
    assert active_one is not None and active_one.release_digest == release_one.digest

    payload = {
        "schema_version": 1,
        "deployment_id": "unknown-after-build",
        "release_digest": unapproved.digest,
        "deployment_digest": "f" * 64,
    }
    source.online = True
    rejected = registry.execute(_claim(AgentOperation.PACKAGE_PREPARE, payload, 8), context)
    assert rejected["status"] == "failed"
    assert rejected["error_code"] == "package_operation_failed"
    assert state.generation_for_release("unknown-after-build", unapproved.digest) is None
    assert unapproved.digest not in source.authorized

    pointer = json.loads((tmp_path / "node/pointers/unknown-after-build.json").read_text())
    assert pointer["release_digest"] == release_one.digest
    assert progress and {"phase", "component"} <= set(progress[0])
    assert {"direct:https:native-runtime", "direct:oci:oci-runtime"} <= set(events)
    assert "adapter:infer" in events
    assert update_guard.calls == []
    assert ssh_calls == []
    assert hashlib.sha256((ROOT / "agent/src/vonk_agent/main.py").read_bytes()).hexdigest() == agent_digest_before
    assert all(item["actor"] == "nas-admin@example" for item in audit)
    assert all(len(hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()) == 64 for item in audit)


def test_installed_agent_wires_the_generic_package_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An installed agent must accept package claims without an agent update.

    The test replaces unrelated installed transport/probe dependencies while
    leaving the package engine composition and ``OperationContext`` assembly
    real.
    """
    from vonk_agent import main
    from vonk_agent.config import AgentConfig
    from vonk_agent.main import build_agent

    runtime = SimpleNamespace(
        architecture="x86_64",
        registry_origin="https://registry.example.invalid",
        repository="synthetic/workloads",
        oras=SimpleNamespace(
            executable=tmp_path / "oras",
            sha256="a" * 64,
            version="1.3.3",
            auth_path=tmp_path / "auth.json",
        ),
        tuf=SimpleNamespace(
            metadata_root=tmp_path / "tuf/metadata",
            target_root=tmp_path / "tuf/targets",
        ),
        workload_tuf=SimpleNamespace(
            metadata_root=tmp_path / "workload-tuf/metadata",
            target_root=tmp_path / "workload-tuf/targets",
        ),
        release_root=tmp_path / "releases",
        staging_root=tmp_path / "staging",
        allow_unprivileged_test_files=True,
        read_bootstrap_root=lambda: b"{}",
        read_workload_bootstrap_root=lambda: b"{}",
        verify_installed=lambda: None,
    )
    for name in (
        "AgentClient",
        "AgentStateStore",
        "BoundedHTTPSFetcher",
        "TUFReleaseTrust",
        "ORASClient",
        "ORASPolicy",
        "ReleaseInstaller",
        "WorkloadOperations",
        "PlatformTUFRouteFetcher",
        "UpdateTrust",
        "PlatformAgentTrust",
        "AgentUpdater",
        "ORASAgentTransport",
        "LocalSupervisor",
        "PinnedNodeProbe",
    ):
        monkeypatch.setattr(main, name, lambda *args, **kwargs: object())
    monkeypatch.setattr(main.InstalledPolicy, "load", lambda _path: object())
    monkeypatch.setattr(main.RuntimePolicy, "load", lambda _path: runtime)
    monkeypatch.setattr(main, "WorkloadTUFSource", lambda *args, **kwargs: object())
    monkeypatch.setattr(main, "WorkloadTrust", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        main.AgentRuntimeIdentity,
        "from_environment",
        classmethod(lambda cls: object()),
    )

    agent = build_agent(
        AgentConfig(
            control_origin="https://control.example",
            enrollment_origin="https://enroll.example",
            node_id="spk_0123456789abcdef0123456789abcdef",
            certificate_path=tmp_path / "cert.pem",
            private_key_path=tmp_path / "key.pem",
            ca_path=tmp_path / "ca.pem",
            poll_min_seconds=1,
            poll_max_seconds=2,
            state_root=tmp_path / "state",
            installed_policy_path=tmp_path / "installed-policy.json",
            runtime_policy_path=tmp_path / "runtime-policy.json",
            enrollment_token_path=tmp_path / "enrollment-token",
        ),
        credentials=object(),
        readiness=SimpleNamespace(report=lambda: None),
    )

    assert agent._context.packages is not None
