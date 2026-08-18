from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from vonk_agent.package_trust import (
    TrustedWorkloadTarget,
    WorkloadTrust,
    WorkloadTrustError,
)
from vonk_agent_protocol import PackageReleaseLock


def package_lock() -> PackageReleaseLock:
    component = {
        "name": "payload",
        "kind": "artifact",
        "media_type": "application/octet-stream",
        "sources": [
            {
                "provider": "https",
                "url": "https://packages.example.invalid/payload.bin",
            }
        ],
        "digest": "sha256:" + "1" * 64,
        "size": 1024,
        "unpacked_size": 1024,
        "platforms": ["linux/arm64"],
        "materialization": {"method": "file"},
        "evidence": [{"kind": "checksum", "digest": "sha256:" + "2" * 64}],
    }
    return PackageReleaseLock.parse(
        {
            "schema_version": 1,
            "family_id": "unknown-future-family",
            "upstream_version": "2026.08-test",
            "upstream_identity": {
                "provider": "git",
                "repository": "https://git.example.invalid/future.git",
                "commit": "3" * 40,
            },
            "components": [component],
            "dependency_digests": [],
            "adapter": component
            | {
                "name": "adapter",
                "kind": "adapter",
                "media_type": "application/vnd.vonk-forge.workload-adapter.v1",
                "materialization": {"method": "executable"},
            },
            "adapter_abi": 1,
            "compatibility": {
                "architectures": ["arm64"],
                "operating_systems": ["linux"],
                "required_capabilities": ["recipe-runtime-v1"],
                "minimum_storage_bytes": 2048,
            },
            "validation": [{"kind": "component-digest", "component": "payload"}],
            "provenance": [{"kind": "slsa", "digest": "sha256:" + "4" * 64}],
            "resolver": {"name": "metadata-v1", "version": 1},
        }
    )


class RecordingSource:
    def __init__(self, target: TrustedWorkloadTarget) -> None:
        self.target = target
        self.refreshes = 0
        self.names: list[str] = []

    def refresh(self) -> None:
        self.refreshes += 1

    def trusted_target(self, name: str) -> TrustedWorkloadTarget:
        self.names.append(name)
        return self.target


def source_for(lock: PackageReleaseLock) -> RecordingSource:
    return RecordingSource(
        TrustedWorkloadTarget(
            name=f"releases/{lock.digest}.json",
            length=len(lock.canonical_bytes),
            sha256=lock.digest,
            data=lock.canonical_bytes,
        )
    )


def test_workload_trust_refreshes_separate_source_and_returns_unknown_family_lock() -> None:
    lock = package_lock()
    source = source_for(lock)
    trust = WorkloadTrust(source)

    trust.refresh()
    trusted = trust.trusted_lock(lock.digest)

    assert source.refreshes == 1
    assert source.names == [f"releases/{lock.digest}.json"]
    assert trusted.digest == lock.digest
    assert trusted.family_id == "unknown-future-family"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda target: replace(target, name="platform/releases/1.2.3/" + "a" * 64 + ".json"),
        lambda target: replace(target, sha256="f" * 64),
        lambda target: replace(target, length=target.length + 1),
        lambda target: replace(target, data=target.data + b" "),
    ],
)
def test_platform_or_inconsistent_targets_cannot_enter_workload_trust(mutate) -> None:
    lock = package_lock()
    source = source_for(lock)
    source.target = mutate(source.target)

    with pytest.raises(WorkloadTrustError, match="target"):
        WorkloadTrust(source).trusted_lock(lock.digest)


def test_requested_digest_must_equal_complete_canonical_lock_digest() -> None:
    lock = package_lock()

    with pytest.raises(WorkloadTrustError, match="target"):
        WorkloadTrust(source_for(lock)).trusted_lock("f" * 64)

    assert hashlib.sha256(lock.canonical_bytes).hexdigest() == lock.digest
