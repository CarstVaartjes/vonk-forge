from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from securesystemslib.signer import CryptoSigner
from tuf.api.metadata import Metadata
from vonk_agent_protocol import PackageReleaseLock
from vonk_control.workload_trust import (
    WorkloadOnlineSigners,
    WorkloadTrustDelivery,
    WorkloadTrustError,
    WorkloadTrustPublisher,
    WorkloadTrustSigners,
    initialize_workload_trust,
    rotate_workload_root,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
COMMIT = "a" * 40
PROVENANCE_DIGEST = "b" * 64
SBOM_DIGEST = "c" * 64


def _lock_bytes(*, family_id: str = "synthetic-family") -> bytes:
    component = {
        "digest": "sha256:" + "1" * 64,
        "evidence": [{"digest": "sha256:" + "2" * 64, "kind": "checksum"}],
        "kind": "artifact",
        "materialization": {"method": "file"},
        "media_type": "application/octet-stream",
        "name": "payload",
        "platforms": ["linux/arm64"],
        "size": 1024,
        "sources": [
            {
                "provider": "https",
                "url": "https://packages.example.invalid/payload.bin",
            }
        ],
        "unpacked_size": 1024,
    }
    adapter = dict(component)
    adapter.update(
        {
            "kind": "adapter",
            "materialization": {"method": "executable"},
            "media_type": "application/vnd.vonk-forge.workload-adapter.v1",
            "name": "adapter",
        }
    )
    return (
        json.dumps(
            {
                "adapter": adapter,
                "adapter_abi": 1,
                "compatibility": {
                    "architectures": ["arm64"],
                    "minimum_storage_bytes": 2048,
                    "operating_systems": ["linux"],
                    "required_capabilities": ["package-abi-v1"],
                },
                "components": [component],
                "dependency_digests": [],
                "family_id": family_id,
                "provenance": [{"digest": "sha256:" + "3" * 64, "kind": "slsa"}],
                "resolver": {"name": "metadata-v1", "version": 1},
                "schema_version": 1,
                "upstream_identity": {
                    "commit": "4" * 40,
                    "provider": "git",
                    "repository": "https://git.example.invalid/synthetic.git",
                },
                "upstream_version": "2026.08-test",
                "validation": [{"component": "payload", "kind": "component-digest"}],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    ).encode("utf-8")


def _package_lock() -> PackageReleaseLock:
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
            "family_id": "synthetic-family",
            "upstream_version": "2026.08-test",
            "upstream_identity": {
                "provider": "git",
                "repository": "https://git.example.invalid/synthetic.git",
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
                "required_capabilities": ["package-abi-v1"],
                "minimum_storage_bytes": 2048,
            },
            "validation": [{"kind": "component-digest", "component": "payload"}],
            "provenance": [{"kind": "slsa", "digest": "sha256:" + "4" * 64}],
            "resolver": {"name": "metadata-v1", "version": 1},
        }
    )


def _evidence(lock_bytes: bytes) -> dict[str, object]:
    return {
        "lock_digest": hashlib.sha256(lock_bytes).hexdigest(),
        "provenance_digest": PROVENANCE_DIGEST,
        "sbom_digest": SBOM_DIGEST,
        "schema_version": 1,
    }


def _signers() -> WorkloadTrustSigners:
    generated = {
        role: CryptoSigner.generate_ed25519()
        for role in (
            "root",
            "targets",
            "snapshot",
            "timestamp",
            "families",
            "releases",
        )
    }
    return WorkloadTrustSigners(**generated)


def _publisher(
    tmp_path: Path,
    *,
    now: datetime = NOW,
    authorized: bool = True,
) -> tuple[WorkloadTrustPublisher, WorkloadTrustSigners, Path, Path]:
    metadata_root = tmp_path / "workload-tuf/metadata"
    target_root = tmp_path / "workload-tuf/targets"
    signers = _signers()
    initialize_workload_trust(
        metadata_root=metadata_root,
        target_root=target_root,
        signers=signers,
        now=now,
    )
    publisher = WorkloadTrustPublisher(
        metadata_root=metadata_root,
        target_root=target_root,
        signers=WorkloadOnlineSigners(
            releases=signers.releases,
            snapshot=signers.snapshot,
            timestamp=signers.timestamp,
        ),
        commit_eligible=lambda commit: commit == COMMIT,
        policy_authorized=(
            lambda family_id, evidence: (
                authorized
                and family_id == "synthetic-family"
                and evidence["provenance_digest"] == PROVENANCE_DIGEST
            )
        ),
        evidence_verified=lambda digest, evidence: (
            evidence["lock_digest"] == digest and evidence["sbom_digest"] == SBOM_DIGEST
        ),
        clock=lambda: now,
    )
    return publisher, signers, metadata_root, target_root


def test_publish_authorizes_exact_lock_under_workload_only_delegation(
    tmp_path: Path,
) -> None:
    publisher, _signers_value, metadata_root, target_root = _publisher(tmp_path)
    lock_bytes = _lock_bytes()
    digest = hashlib.sha256(lock_bytes).hexdigest()

    trusted = publisher.publish(lock_bytes, COMMIT, _evidence(lock_bytes))

    assert trusted.digest == digest
    assert trusted.length == len(lock_bytes)
    assert trusted.git_commit == COMMIT
    assert trusted.tuf_snapshot_version == 2
    assert (target_root / digest).read_bytes() == lock_bytes

    targets = Metadata.from_bytes((metadata_root / "targets.json").read_bytes())
    releases = Metadata.from_bytes((metadata_root / "releases.json").read_bytes())
    targets.verify_delegate("releases", releases)
    descriptor = releases.signed.targets[f"releases/{digest}.json"]
    assert descriptor.length == len(lock_bytes)
    assert descriptor.hashes == {"sha256": digest}


def test_shared_package_lock_bytes_are_the_exact_workload_tuf_target(
    tmp_path: Path,
) -> None:
    publisher, _signers_value, metadata_root, target_root = _publisher(tmp_path)
    lock = _package_lock()

    trusted = publisher.publish(
        lock.canonical_bytes,
        COMMIT,
        _evidence(lock.canonical_bytes),
    )
    delivered = WorkloadTrustDelivery(
        metadata_root=metadata_root,
        target_root=target_root,
    ).target(trusted.digest)

    assert trusted.digest == lock.digest
    assert delivered == lock.canonical_bytes
    assert hashlib.sha256(delivered).hexdigest() == lock.digest


def test_delivery_authorizes_release_only_for_exact_git_commit(
    tmp_path: Path,
) -> None:
    publisher, _signers_value, metadata_root, target_root = _publisher(tmp_path)
    lock_bytes = _lock_bytes()
    trusted = publisher.publish(lock_bytes, COMMIT, _evidence(lock_bytes))
    delivery = WorkloadTrustDelivery(
        metadata_root=metadata_root,
        target_root=target_root,
    )

    assert delivery.authorize_release(trusted.digest, lock_bytes, COMMIT) is True
    assert delivery.authorize_release(trusted.digest, lock_bytes, "d" * 40) is False
    assert delivery.authorize_release("0" * 64, lock_bytes, COMMIT) is False
    assert delivery.authorize_release(trusted.digest, lock_bytes + b"x", COMMIT) is False


@pytest.mark.parametrize(
    "target_name",
    (
        "platform/releases/1.2.3/artifact.json",
        "agent/slots/arm64",
        "supervisor/releases/1",
        "protocol/v3",
        "node-policy/default",
        "families/synthetic-family",
        "releases/latest.json",
        "releases/../../platform.json",
    ),
)
def test_workload_release_key_cannot_publish_outside_digest_release_delegation(
    tmp_path: Path,
    target_name: str,
) -> None:
    publisher, _signers_value, _metadata_root, _target_root = _publisher(tmp_path)
    lock_bytes = _lock_bytes()

    with pytest.raises(WorkloadTrustError, match="outside workload delegation"):
        publisher.publish_as(target_name, lock_bytes, COMMIT, _evidence(lock_bytes))


@pytest.mark.parametrize("fault", ("commit", "policy", "evidence", "canonical"))
def test_publish_requires_eligible_commit_policy_verified_evidence_and_canonical_lock(
    tmp_path: Path,
    fault: str,
) -> None:
    publisher, _signers_value, _metadata_root, _target_root = _publisher(
        tmp_path,
        authorized=fault != "policy",
    )
    lock_bytes = _lock_bytes()
    commit = "d" * 40 if fault == "commit" else COMMIT
    evidence = _evidence(lock_bytes)
    if fault == "evidence":
        evidence["sbom_digest"] = "e" * 64
    if fault == "canonical":
        lock_bytes = b'{"schema_version": 1, "family_id": "synthetic-family"}\n'

    with pytest.raises(WorkloadTrustError, match=fault):
        publisher.publish(lock_bytes, commit, evidence)


def test_publication_rejects_expired_or_mix_and_match_repository_metadata(
    tmp_path: Path,
) -> None:
    publisher, signers, metadata_root, target_root = _publisher(tmp_path)
    old_snapshot = (metadata_root / "1.snapshot.json").read_bytes()
    lock_bytes = _lock_bytes()
    publisher.publish(lock_bytes, COMMIT, _evidence(lock_bytes))

    expired = WorkloadTrustPublisher(
        metadata_root=metadata_root,
        target_root=target_root,
        signers=WorkloadOnlineSigners(
            releases=signers.releases,
            snapshot=signers.snapshot,
            timestamp=signers.timestamp,
        ),
        commit_eligible=lambda _commit: True,
        policy_authorized=lambda _family, _evidence: True,
        evidence_verified=lambda _digest, _evidence: True,
        clock=lambda: NOW + timedelta(days=8),
    )
    with pytest.raises(WorkloadTrustError, match="expired"):
        expired.publish(lock_bytes, COMMIT, _evidence(lock_bytes))

    activated_snapshot = metadata_root / "2.snapshot.json"
    activated_snapshot.chmod(0o644)
    activated_snapshot.write_bytes(old_snapshot)
    activated_snapshot.chmod(0o444)
    with pytest.raises(WorkloadTrustError, match="mix-and-match"):
        publisher.publish(lock_bytes, COMMIT, _evidence(lock_bytes))


def test_publisher_rejects_metadata_rollback_against_durable_version_floor(
    tmp_path: Path,
) -> None:
    publisher, signers, metadata_root, target_root = _publisher(tmp_path)
    original_timestamp = (metadata_root / "timestamp.json").read_bytes()
    first = _lock_bytes()
    publisher.publish(first, COMMIT, _evidence(first))
    timestamp = metadata_root / "timestamp.json"
    timestamp.chmod(0o644)
    timestamp.write_bytes(original_timestamp)
    timestamp.chmod(0o444)

    restarted = WorkloadTrustPublisher(
        metadata_root=metadata_root,
        target_root=target_root,
        signers=WorkloadOnlineSigners(
            releases=signers.releases,
            snapshot=signers.snapshot,
            timestamp=signers.timestamp,
        ),
        commit_eligible=lambda _commit: True,
        policy_authorized=lambda _family, _evidence: True,
        evidence_verified=lambda _digest, _evidence: True,
        clock=lambda: NOW,
    )
    with pytest.raises(WorkloadTrustError, match="rollback"):
        restarted.publish(first, COMMIT, _evidence(first))


def test_root_rotation_is_signed_by_old_and_new_workload_roots(tmp_path: Path) -> None:
    _publisher_value, signers, metadata_root, _target_root = _publisher(tmp_path)
    old = Metadata.from_bytes((metadata_root / "root.json").read_bytes())
    replacement = CryptoSigner.generate_ed25519()

    rotated_bytes = rotate_workload_root(
        metadata_root=metadata_root,
        current_signer=signers.root,
        replacement_signer=replacement,
        now=NOW,
    )
    rotated = Metadata.from_bytes(rotated_bytes)

    old.verify_delegate("root", rotated)
    rotated.verify_delegate("root", rotated)
    assert rotated.signed.version == 2
    assert (metadata_root / "2.root.json").read_bytes() == rotated_bytes


def test_root_rotation_contends_on_the_workload_publication_lock(
    tmp_path: Path,
) -> None:
    _publisher_value, signers, metadata_root, _target_root = _publisher(tmp_path)
    descriptor = os.open(metadata_root / ".publish.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(WorkloadTrustError, match="publication is active"):
            rotate_workload_root(
                metadata_root=metadata_root,
                current_signer=signers.root,
                replacement_signer=CryptoSigner.generate_ed25519(),
                now=NOW,
            )
    finally:
        os.close(descriptor)


def test_commit_policy_and_evidence_authority_are_checked_under_publication_lock(
    tmp_path: Path,
) -> None:
    metadata_root = tmp_path / "workload-tuf/metadata"
    target_root = tmp_path / "workload-tuf/targets"
    signers = _signers()
    initialize_workload_trust(
        metadata_root=metadata_root,
        target_root=target_root,
        signers=signers,
        now=NOW,
    )
    lock_observations: list[bool] = []

    def authority_check(*_args: object) -> bool:
        descriptor = os.open(
            metadata_root / ".publish.lock", os.O_RDWR | os.O_CREAT, 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_observations.append(True)
            else:
                lock_observations.append(False)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
        return True

    publisher = WorkloadTrustPublisher(
        metadata_root=metadata_root,
        target_root=target_root,
        signers=WorkloadOnlineSigners(
            releases=signers.releases,
            snapshot=signers.snapshot,
            timestamp=signers.timestamp,
        ),
        commit_eligible=authority_check,
        policy_authorized=authority_check,
        evidence_verified=authority_check,
        clock=lambda: NOW,
    )
    lock_bytes = _lock_bytes()

    publisher.publish(lock_bytes, COMMIT, _evidence(lock_bytes))

    assert lock_observations == [True, True, True]


def test_idempotent_republish_restores_missing_digest_addressed_target(
    tmp_path: Path,
) -> None:
    publisher, _signers_value, _metadata_root, target_root = _publisher(tmp_path)
    lock_bytes = _lock_bytes()
    trusted = publisher.publish(lock_bytes, COMMIT, _evidence(lock_bytes))
    (target_root / trusted.digest).unlink()

    repeated = publisher.publish(lock_bytes, COMMIT, _evidence(lock_bytes))

    assert repeated == trusted
    assert (target_root / trusted.digest).read_bytes() == lock_bytes


def test_delivery_is_digest_exact_bounded_and_rejects_unsafe_files(
    tmp_path: Path,
) -> None:
    publisher, _signers_value, metadata_root, target_root = _publisher(tmp_path)
    lock_bytes = _lock_bytes()
    trusted = publisher.publish(lock_bytes, COMMIT, _evidence(lock_bytes))
    delivery = WorkloadTrustDelivery(
        metadata_root=metadata_root,
        target_root=target_root,
        max_metadata_bytes=1024 * 1024,
        max_target_bytes=len(lock_bytes),
    )

    assert (
        delivery.metadata("timestamp")
        == (metadata_root / "timestamp.json").read_bytes()
    )
    assert delivery.target(trusted.digest) == lock_bytes
    with pytest.raises(WorkloadTrustError, match="digest"):
        delivery.target("0" * 64)

    target = target_root / trusted.digest
    target.chmod(0o644)
    target.write_bytes(lock_bytes + b"x")
    target.chmod(0o444)
    with pytest.raises(WorkloadTrustError, match="size"):
        delivery.target(trusted.digest)

    target.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(lock_bytes)
    target.symlink_to(outside)
    with pytest.raises(WorkloadTrustError, match="unsafe"):
        delivery.target(trusted.digest)
