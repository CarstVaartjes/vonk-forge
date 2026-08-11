from __future__ import annotations

import builtins
import fcntl
import hashlib
import json
import os
import select
import socket
import ssl
import stat
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import vonk_agent.oci as oci_module
import vonk_agent.releases as release_module
from securesystemslib.signer import CryptoSigner
from tuf.api.exceptions import DownloadError, DownloadHTTPError
from tuf.api.metadata import (
    DelegatedRole,
    Delegations,
    Metadata,
    MetaFile,
    Root,
    Snapshot,
    TargetFile,
    Targets,
    Timestamp,
)
from tuf.ngclient import FetcherInterface
from tuf.ngclient._internal.trusted_metadata_set import TrustedMetadataSet
from vonk_agent import nvidia_tools, update_trust
from vonk_agent.client import CredentialSnapshot
from vonk_agent.deadlines import DeadlineBindingError, MonotonicDeadline
from vonk_agent.oci import OCIError, ORASClient, ORASPolicy
from vonk_agent.probe import ProcessOutcome
from vonk_agent.releases import (
    ReleaseDescriptor,
    ReleaseDisposition,
    ReleaseEvidence,
    ReleaseInspection,
    ReleaseInstaller,
    ReleaseInstallError,
    ReleaseRequest,
    ReleaseValidationError,
    verify_installed_release,
    verify_release_tree,
)
from vonk_agent.update_trust import BoundedHTTPSFetcher, TUFReleaseTrust, TUFTrustError

VALID_RELEASE = {
    "schema_version": 1,
    "target_name": "node-runtime-2026-08",
    "oci_manifest_digest": "sha256:" + "1" * 64,
    "target_digest": "2" * 64,
    "provenance_digest": "3" * 64,
    "adapter_id": "node-runtime-v1",
}


def _descriptor() -> dict[str, object]:
    return {
        "schema_version": 1,
        "target_name": VALID_RELEASE["target_name"],
        "target_digest": VALID_RELEASE["target_digest"],
        "target_length": 17,
        "registry_origin": "https://registry.test.example",
        "repository": "vonk/releases",
        "oci_manifest_digest": VALID_RELEASE["oci_manifest_digest"],
        "provenance_digest": VALID_RELEASE["provenance_digest"],
        "adapter_id": VALID_RELEASE["adapter_id"],
        "adapter_version": "1.0.0",
        "architecture": "linux-arm64",
        "agent_min_version": "0.1.0",
        "agent_max_version": "0.1.0",
        "protocol_min_version": 1,
        "protocol_max_version": 1,
        "members": [
            {
                "path": "bin/runtime-adapter",
                "sha256": hashlib.sha256(b"x" * 17).hexdigest(),
                "size": 17,
                "mode": 0o500,
                "uid": __import__("os").geteuid(),
                "gid": __import__("os").getegid(),
            }
        ],
    }


class RepositoryFetcher(FetcherInterface):
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.urls: list[str] = []
        self.deadline = float("inf")

    def set_deadline(self, absolute_monotonic: float) -> None:
        self.deadline = absolute_monotonic

    def _fetch(self, url: str):
        self.urls.append(url)
        name = urlsplit(url).path.rsplit("/", 1)[-1]
        try:
            yield self.files[name]
        except KeyError as error:
            raise DownloadHTTPError("missing", 404) from error


def _signed_repository(
    descriptor: dict[str, object], *, expired: bool = False,
    bad_threshold: bool = False, version: int = 1,
    signers: dict[str, CryptoSigner] | None = None,
    root_bytes: bytes | None = None,
    target_length_override: int | None = None,
) -> tuple[bytes, RepositoryFetcher]:
    expiry = datetime.now(UTC) + (-timedelta(days=1) if expired else timedelta(days=1))
    signers = signers or {
        role: CryptoSigner.generate_ed25519()
        for role in ("root", "timestamp", "snapshot", "targets")
    }
    if root_bytes is None:
        root = Root(expires=expiry, consistent_snapshot=False)
        for role, signer in signers.items():
            root.add_key(signer.public_key, role)
        if bad_threshold:
            second_targets = CryptoSigner.generate_ed25519()
            root.add_key(second_targets.public_key, "targets")
            root.roles["targets"].threshold = 2
        root_metadata = Metadata(root)
        root_metadata.sign(signers["root"])
        root_bytes = root_metadata.to_bytes()

    target_bytes = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
    target = (
        TargetFile(
            target_length_override,
            {"sha256": hashlib.sha256(target_bytes).hexdigest()},
            str(descriptor["target_name"]),
        )
        if target_length_override is not None
        else TargetFile.from_data(
            str(descriptor["target_name"]), target_bytes, ["sha256"]
        )
    )
    target.unrecognized_fields["custom"] = {"release": descriptor}
    targets_metadata = Metadata(
        Targets(version=version, expires=expiry, targets={str(descriptor["target_name"]): target})
    )
    targets_metadata.sign(signers["targets"])
    targets_bytes = targets_metadata.to_bytes()

    snapshot_metadata = Metadata(
        Snapshot(
            version=version,
            expires=expiry,
            meta={"targets.json": MetaFile.from_data(version, targets_bytes, ["sha256"])},
        )
    )
    snapshot_metadata.sign(signers["snapshot"])
    snapshot_bytes = snapshot_metadata.to_bytes()
    timestamp_metadata = Metadata(
        Timestamp(
            version=version,
            expires=expiry,
            snapshot_meta=MetaFile.from_data(version, snapshot_bytes, ["sha256"]),
        )
    )
    timestamp_metadata.sign(signers["timestamp"])
    fetcher = RepositoryFetcher(
        {
            "timestamp.json": timestamp_metadata.to_bytes(),
            "snapshot.json": snapshot_bytes,
            "targets.json": targets_bytes,
            str(descriptor["target_name"]): target_bytes,
        }
    )
    fetcher.signers = signers
    return root_bytes, fetcher


def _rotated_repository(invalid: bool = False) -> tuple[bytes, RepositoryFetcher]:
    expiry = datetime.now(UTC) + timedelta(days=1)
    old_root = CryptoSigner.generate_ed25519()
    new_root = CryptoSigner.generate_ed25519()
    signers = {
        role: CryptoSigner.generate_ed25519()
        for role in ("timestamp", "snapshot", "targets")
    }
    root1 = Root(version=1, expires=expiry, consistent_snapshot=False)
    root1.add_key(old_root.public_key, "root")
    for role, signer in signers.items():
        root1.add_key(signer.public_key, role)
    root1_metadata = Metadata(root1)
    root1_metadata.sign(old_root)
    root1_bytes = root1_metadata.to_bytes()

    root2 = Root(version=2, expires=expiry, consistent_snapshot=False)
    root2.add_key(new_root.public_key, "root")
    for role, signer in signers.items():
        root2.add_key(signer.public_key, role)
    root2_metadata = Metadata(root2)
    if not invalid:
        root2_metadata.sign(old_root)
    root2_metadata.sign(new_root, append=True)

    _, fetcher = _signed_repository(
        _descriptor(), signers={"root": new_root, **signers}, root_bytes=root1_bytes
    )
    fetcher.files["2.root.json"] = root2_metadata.to_bytes()
    return root1_bytes, fetcher


def _delegated_repository(invalid: bool = False) -> tuple[bytes, RepositoryFetcher]:
    descriptor = _descriptor()
    root_bytes, fetcher = _signed_repository(descriptor)
    expiry = datetime.now(UTC) + timedelta(days=1)
    delegated_signer = CryptoSigner.generate_ed25519()
    target_bytes = (json.dumps(descriptor, sort_keys=True, separators=(",", ":")) + "\n").encode()
    target = TargetFile.from_data(str(descriptor["target_name"]), target_bytes, ["sha256"])
    target.unrecognized_fields["custom"] = {"release": descriptor}
    delegated = Metadata(
        Targets(expires=expiry, targets={str(descriptor["target_name"]): target})
    )
    delegated.sign(delegated_signer)
    delegated_bytes = delegated.to_bytes()
    role = DelegatedRole(
        "node", [delegated_signer.public_key.keyid], 2 if invalid else 1,
        True, paths=[str(descriptor["target_name"])],
    )
    top = Metadata(
        Targets(
            expires=expiry,
            targets={},
            delegations=Delegations(
                {delegated_signer.public_key.keyid: delegated_signer.public_key},
                {"node": role},
            ),
        )
    )
    top.sign(fetcher.signers["targets"])
    top_bytes = top.to_bytes()
    snapshot = Metadata(
        Snapshot(
            expires=expiry,
            meta={
                "targets.json": MetaFile.from_data(1, top_bytes, ["sha256"]),
                "node.json": MetaFile.from_data(1, delegated_bytes, ["sha256"]),
            },
        )
    )
    snapshot.sign(fetcher.signers["snapshot"])
    snapshot_bytes = snapshot.to_bytes()
    timestamp = Metadata(
        Timestamp(
            expires=expiry,
            snapshot_meta=MetaFile.from_data(1, snapshot_bytes, ["sha256"]),
        )
    )
    timestamp.sign(fetcher.signers["timestamp"])
    fetcher.files.update({
        "timestamp.json": timestamp.to_bytes(),
        "snapshot.json": snapshot_bytes,
        "targets.json": top_bytes,
        "node.json": delegated_bytes,
        str(descriptor["target_name"]): target_bytes,
    })
    return root_bytes, fetcher


def test_release_request_accepts_only_the_exact_versioned_digest_boundary() -> None:
    request = ReleaseRequest.parse(VALID_RELEASE)

    assert request.target_name == "node-runtime-2026-08"
    assert request.oci_manifest_digest == "sha256:" + "1" * 64
    assert request.target_digest == "2" * 64
    assert request.provenance_digest == "3" * 64
    assert request.adapter_id == "node-runtime-v1"

    for changed in (
        VALID_RELEASE | {"command": ["id"]},
        VALID_RELEASE | {"registry": "https://attacker.invalid"},
        VALID_RELEASE | {"target_name": "../release"},
        VALID_RELEASE | {"oci_manifest_digest": "latest"},
        VALID_RELEASE | {"target_digest": "A" * 64},
        {key: value for key, value in VALID_RELEASE.items() if key != "adapter_id"},
    ):
        with pytest.raises(ReleaseValidationError):
            ReleaseRequest.parse(changed)


def test_release_evidence_and_inspection_are_bounded_typed_values() -> None:
    evidence = ReleaseEvidence(
        status="installed",
        release_digest="2" * 64,
        manifest_digest="sha256:" + "1" * 64,
        adapter_id="node-runtime-v1",
    )
    inspection = ReleaseInspection(ReleaseDisposition.COMPLETED, evidence)

    assert evidence.to_mapping() == {
        "status": "installed",
        "release_digest": "2" * 64,
        "manifest_digest": "sha256:" + "1" * 64,
        "adapter_id": "node-runtime-v1",
    }
    assert inspection.evidence is evidence


def test_real_tuf_updater_authorizes_exact_signed_release_descriptor(tmp_path: Path) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    trust = TUFReleaseTrust(
        metadata_root=tmp_path / "metadata",
        target_root=tmp_path / "targets",
        metadata_base_url="https://control.test.example/agent/v1/tuf/metadata/",
        target_base_url="https://control.test.example/agent/v1/tuf/targets/",
        bootstrap_root=root_bytes,
        fetcher=fetcher,
        registry_origin="https://registry.test.example",
        repository="vonk/releases",
        architecture="linux-arm64",
    )

    authorized = trust.authorize(
        ReleaseRequest.parse(VALID_RELEASE), datetime.now(UTC) + timedelta(seconds=2)
    )

    assert isinstance(authorized, ReleaseDescriptor)
    assert authorized.target_digest == "2" * 64
    assert authorized.members[0].path == "bin/runtime-adapter"
    assert fetcher.urls == [
        "https://control.test.example/agent/v1/tuf/metadata/2.root.json",
        "https://control.test.example/agent/v1/tuf/metadata/timestamp.json",
        "https://control.test.example/agent/v1/tuf/metadata/snapshot.json",
        "https://control.test.example/agent/v1/tuf/metadata/targets.json",
        "https://control.test.example/agent/v1/tuf/targets/node-runtime-2026-08",
    ]


def test_real_tuf_rejects_expired_metadata_and_wrong_target_bytes(tmp_path: Path) -> None:
    expired_root, expired_fetcher = _signed_repository(_descriptor(), expired=True)
    with pytest.raises(TUFTrustError):
        TUFReleaseTrust(
            tmp_path / "expired-metadata", tmp_path / "expired-targets",
            "https://control.test.example/agent/v1/tuf/metadata/",
            "https://control.test.example/agent/v1/tuf/targets/",
            expired_root, expired_fetcher,
            "https://registry.test.example", "vonk/releases", "linux-arm64",
        ).authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )

    root_bytes, fetcher = _signed_repository(_descriptor())
    fetcher.files["node-runtime-2026-08"] = b"tampered"
    with pytest.raises(TUFTrustError):
        TUFReleaseTrust(
            tmp_path / "wrong-metadata", tmp_path / "wrong-targets",
            "https://control.test.example/agent/v1/tuf/metadata/",
            "https://control.test.example/agent/v1/tuf/targets/",
            root_bytes, fetcher,
            "https://registry.test.example", "vonk/releases", "linux-arm64",
        ).authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )


def test_real_tuf_rejects_bad_signature_threshold_and_unsafe_cache_root(tmp_path: Path) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor(), bad_threshold=True)
    with pytest.raises(TUFTrustError):
        TUFReleaseTrust(
            tmp_path / "threshold-metadata", tmp_path / "threshold-targets",
            "https://control.test.example/agent/v1/tuf/metadata/",
            "https://control.test.example/agent/v1/tuf/targets/",
            root_bytes, fetcher,
            "https://registry.test.example", "vonk/releases", "linux-arm64",
        ).authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )


def test_tuf_rejects_oversized_signed_target_before_target_download(tmp_path: Path) -> None:
    root_bytes, fetcher = _signed_repository(
        _descriptor(), target_length_override=1024 * 1024 + 1
    )
    with pytest.raises(TUFTrustError, match="bounds"):
        TUFReleaseTrust(
            tmp_path / "metadata", tmp_path / "targets",
            "https://control.test.example/agent/v1/tuf/metadata/",
            "https://control.test.example/agent/v1/tuf/targets/",
            root_bytes, fetcher,
            "https://registry.test.example", "vonk/releases", "linux-arm64",
        ).authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )
    assert not any(url.endswith("node-runtime-2026-08") for url in fetcher.urls)


def test_tuf_target_memfd_is_write_sealed_before_descriptor_parsing() -> None:
    descriptor = os.memfd_create(
        "target-seal-test", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    )
    try:
        os.write(descriptor, b"signed target")
        update_trust._seal_target_fd(descriptor)
        seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
        assert seals & fcntl.F_SEAL_WRITE
        assert seals & fcntl.F_SEAL_GROW
        assert seals & fcntl.F_SEAL_SHRINK
        with pytest.raises(OSError):
            os.write(descriptor, b"tamper")
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("attack", ["rollback", "freeze", "mix-and-match"])
def test_real_tuf_rejects_version_and_consistency_attacks(tmp_path: Path, attack: str) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor(), version=2)
    trust = TUFReleaseTrust(
        tmp_path / attack / "metadata", tmp_path / attack / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))

    replacement_root, replacement = _signed_repository(
        _descriptor(),
        version=1 if attack == "rollback" else 3,
        expired=attack == "freeze",
        signers=fetcher.signers,
        root_bytes=root_bytes,
    )
    assert replacement_root == root_bytes
    if attack == "mix-and-match":
        replacement.files["snapshot.json"] = fetcher.files["snapshot.json"]
    fetcher.files.update(replacement.files)

    with pytest.raises(TUFTrustError):
        trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))


@pytest.mark.parametrize(
    ("repository_factory", "accepted"),
    [
        (lambda: _rotated_repository(False), True),
        (lambda: _rotated_repository(True), False),
        (lambda: _delegated_repository(False), True),
        (lambda: _delegated_repository(True), False),
    ],
)
def test_real_tuf_enforces_root_rotation_and_delegation_thresholds(
    tmp_path: Path, repository_factory, accepted: bool
) -> None:
    root_bytes, fetcher = repository_factory()
    trust = TUFReleaseTrust(
        tmp_path / ("accepted" if accepted else "rejected") / "metadata",
        tmp_path / ("accepted" if accepted else "rejected") / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    if accepted:
        result = trust.authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )
        assert result.target_digest == "2" * 64
    else:
        with pytest.raises(TUFTrustError):
            trust.authorize(
                ReleaseRequest.parse(VALID_RELEASE),
                datetime.now(UTC) + timedelta(seconds=2),
            )



def test_tuf_rejects_symlinked_metadata_and_nonempty_target_cache(tmp_path: Path) -> None:
    actual = tmp_path / "actual-cache"
    actual.mkdir()
    cache = tmp_path / "linked-cache"
    cache.symlink_to(actual, target_is_directory=True)
    root_bytes, fetcher = _signed_repository(_descriptor())
    with pytest.raises(TUFTrustError):
        TUFReleaseTrust(
            cache, tmp_path / "linked-targets",
            "https://control.test.example/agent/v1/tuf/metadata/",
            "https://control.test.example/agent/v1/tuf/targets/",
            root_bytes, fetcher,
            "https://registry.test.example", "vonk/releases", "linux-arm64",
        ).authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )

    target_cache = tmp_path / "target-cache"
    target_cache.mkdir(mode=0o700)
    (target_cache / "node-runtime-2026-08").symlink_to(tmp_path / "victim")
    root_bytes, fetcher = _signed_repository(_descriptor())
    with pytest.raises(TUFTrustError, match="not empty"):
        TUFReleaseTrust(
            tmp_path / "clean-metadata", target_cache,
            "https://control.test.example/agent/v1/tuf/metadata/",
            "https://control.test.example/agent/v1/tuf/targets/",
            root_bytes, fetcher,
            "https://registry.test.example", "vonk/releases", "linux-arm64",
        ).authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )


def test_tuf_cache_is_single_writer_and_second_updater_fails_closed(tmp_path: Path) -> None:
    root_bytes, base = _signed_repository(_descriptor())
    entered = threading.Event()
    release = threading.Event()

    class BlockingFetcher(RepositoryFetcher):
        def _fetch(self, url: str):
            if url.endswith("timestamp.json"):
                entered.set()
                release.wait(2)
            yield from super()._fetch(url)

    fetcher = BlockingFetcher(base.files)
    arguments = (
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    first = TUFReleaseTrust(*arguments)
    second = TUFReleaseTrust(*arguments)
    errors: list[Exception] = []

    thread = threading.Thread(
        target=lambda: first.authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )
    )
    thread.start()
    assert entered.wait(1)
    with pytest.raises(TUFTrustError, match="already in use"):
        second.authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=1),
        )
    release.set()
    thread.join()
    assert errors == []


def test_tuf_interrupted_refresh_fails_closed_and_same_new_version_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_bytes, initial = _signed_repository(_descriptor(), version=2)

    class InterruptingFetcher(RepositoryFetcher):
        fail_snapshot = False

        def _fetch(self, url: str):
            if self.fail_snapshot and url.endswith("snapshot.json"):
                raise DownloadError("injected interrupted refresh")
            yield from super()._fetch(url)

    fetcher = InterruptingFetcher(initial.files)
    fetcher.signers = initial.signers
    trust = TUFReleaseTrust(
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    _, version3 = _signed_repository(
        _descriptor(), version=3, signers=initial.signers, root_bytes=root_bytes
    )
    fetcher.files.update(version3.files)
    fetcher.fail_snapshot = True
    persisted: list[tuple[Path, Path]] = []
    original_persist = update_trust._persist_accepted_cache

    def record_persist(metadata_root, target_root, deadline):
        persisted.append((metadata_root, target_root))
        original_persist(metadata_root, target_root, deadline)

    monkeypatch.setattr(update_trust, "_persist_accepted_cache", record_persist)
    with pytest.raises(TUFTrustError):
        trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    assert persisted == [(tmp_path / "metadata", tmp_path / "targets")]

    fetcher.fail_snapshot = False
    recovered = trust.authorize(
        request, datetime.now(UTC) + timedelta(seconds=2)
    )
    assert recovered.target_digest == "2" * 64


def test_tuf_regular_read_deadline_stops_after_one_slow_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "metadata.json"
    path.write_bytes(b"x" * (3 * 64 * 1024))
    descriptor = os.open(path, os.O_RDONLY)
    original_read = update_trust.os.read
    reads = 0

    def slow_read(fd, size):
        nonlocal reads
        reads += 1
        time.sleep(0.03)
        return original_read(fd, size)

    monkeypatch.setattr(update_trust.os, "read", slow_read)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    try:
        with pytest.raises(TUFTrustError, match="deadline"):
            update_trust._read_regular_fd(descriptor, 1024 * 1024, deadline)
    finally:
        os.close(descriptor)
    assert reads == 1


def test_tuf_cache_persistence_deadline_stops_remaining_tree_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = tmp_path / "metadata"
    targets = tmp_path / "targets"
    for root in (metadata, targets):
        root.mkdir(mode=0o700)
        for name in ("one.json", "two.json", "three.json"):
            (root / name).write_bytes(b"x")
    original_chmod = update_trust.os.chmod
    chmods = 0

    def slow_chmod(*args, **kwargs):
        nonlocal chmods
        chmods += 1
        time.sleep(0.03)
        return original_chmod(*args, **kwargs)

    monkeypatch.setattr(update_trust.os, "chmod", slow_chmod)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    with pytest.raises(TUFTrustError, match="deadline"):
        update_trust._persist_accepted_cache(metadata, targets, deadline)
    assert chmods == 1


def test_tuf_error_persistence_receives_same_expired_deadline_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    trust = TUFReleaseTrust(
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    original_persist = update_trust._persist_accepted_cache
    seen: list[MonotonicDeadline] = []
    hardens: list[Path] = []

    def slow_failed_refresh(self):
        time.sleep(0.03)
        raise DownloadError("injected refresh failure")

    def record_persist(metadata_root, target_root, deadline):
        seen.append(deadline)
        return original_persist(metadata_root, target_root, deadline)

    monkeypatch.setattr(update_trust.Updater, "refresh", slow_failed_refresh)
    monkeypatch.setattr(update_trust, "_persist_accepted_cache", record_persist)
    monkeypatch.setattr(
        update_trust, "_harden_cache",
        lambda root, deadline: hardens.append(root),
    )
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    with pytest.raises(TUFTrustError, match="deadline"):
        trust.authorize(request, deadline)
    assert seen == [deadline]
    assert hardens == []


def test_tuf_deadline_interrupts_signed_metadata_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    trust = TUFReleaseTrust(
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    clock = [0.0]
    writes: list[str] = []
    original_update = TrustedMetadataSet.update_timestamp
    original_persist = update_trust.Updater._persist_file
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])

    def expire_inside_verification(self, data):
        writes.clear()
        clock[0] = 11.0
        return original_update(self, data)

    def record_persist(self, filename, data):
        writes.append(filename)
        return original_persist(self, filename, data)

    monkeypatch.setattr(
        TrustedMetadataSet, "update_timestamp", expire_inside_verification
    )
    monkeypatch.setattr(update_trust.Updater, "_persist_file", record_persist)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=10), 10.0
    )

    with pytest.raises(TUFTrustError, match="deadline"):
        trust.authorize(ReleaseRequest.parse(VALID_RELEASE), deadline)
    assert writes == []


def test_tuf_deadline_interrupts_constructor_after_root_verification_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    trust = TUFReleaseTrust(
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    clock = [0.0]
    persisted: list[int] = []
    original_init = TrustedMetadataSet.__init__
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])

    def verify_root_then_expire(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        clock[0] = 11.0

    def record_root_persistence(self, version, data):
        persisted.append(version)

    monkeypatch.setattr(TrustedMetadataSet, "__init__", verify_root_then_expire)
    monkeypatch.setattr(update_trust.Updater, "_persist_root", record_root_persistence)

    with pytest.raises(TUFTrustError, match="deadline"):
        trust.authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 10.0),
        )
    assert persisted == []
    assert not (tmp_path / "metadata/root.json").exists()


def test_tuf_deadline_interrupts_target_hash_before_destination_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    trust = TUFReleaseTrust(
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    clock = [0.0]
    destination_opens: list[str] = []
    original_verify = TargetFile.verify_length_and_hashes
    original_open = builtins.open
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])

    def expire_inside_hash(self, fileobj):
        clock[0] = 11.0
        return original_verify(self, fileobj)

    def record_open(path, mode="r", *args, **kwargs):
        if mode == "wb":
            destination_opens.append(str(path))
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(TargetFile, "verify_length_and_hashes", expire_inside_hash)
    monkeypatch.setattr(builtins, "open", record_open)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=10), 10.0
    )

    with pytest.raises(TUFTrustError, match="deadline"):
        trust.authorize(ReleaseRequest.parse(VALID_RELEASE), deadline)
    assert destination_opens == []


@pytest.mark.parametrize("phase", ["signed-descriptor", "target-json"])
def test_tuf_deadline_stops_between_local_parse_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    trust = TUFReleaseTrust(
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    clock = [0.0]
    target_memfds: list[str] = []
    descriptor_parses = 0
    original_parse = ReleaseDescriptor.parse
    original_unique = update_trust._unique_object
    original_memfd = update_trust.os.memfd_create
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])

    def parse_then_expire(document):
        nonlocal descriptor_parses
        descriptor_parses += 1
        parsed = original_parse(document)
        if phase == "signed-descriptor" and descriptor_parses == 1:
            clock[0] = 11.0
        return parsed

    def unique_then_expire(pairs):
        parsed = original_unique(pairs)
        if phase == "target-json":
            clock[0] = 11.0
        return parsed

    def record_memfd(name, flags):
        target_memfds.append(name)
        return original_memfd(name, flags)

    monkeypatch.setattr(ReleaseDescriptor, "parse", parse_then_expire)
    monkeypatch.setattr(update_trust, "_unique_object", unique_then_expire)
    monkeypatch.setattr(update_trust.os, "memfd_create", record_memfd)

    with pytest.raises(TUFTrustError, match="deadline"):
        trust.authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 10.0),
        )

    if phase == "signed-descriptor":
        assert target_memfds == []
    else:
        assert descriptor_parses == 1


def test_tuf_deadline_after_target_memfd_creation_starts_no_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    trust = TUFReleaseTrust(
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    clock = [0.0]
    downloads = 0
    original_memfd = update_trust.os.memfd_create
    original_download = update_trust.Updater.download_target
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])

    def create_then_expire(name, flags):
        descriptor = original_memfd(name, flags)
        if name == "vonk-tuf-target":
            clock[0] = 11.0
        return descriptor

    def record_download(self, *args, **kwargs):
        nonlocal downloads
        downloads += 1
        return original_download(self, *args, **kwargs)

    monkeypatch.setattr(update_trust.os, "memfd_create", create_then_expire)
    monkeypatch.setattr(update_trust.Updater, "download_target", record_download)
    with pytest.raises(TUFTrustError, match="deadline"):
        trust.authorize(
            ReleaseRequest.parse(VALID_RELEASE),
            MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 10.0),
        )
    assert downloads == 0


def test_tuf_deadline_hook_restores_thread_trace_on_success_and_error() -> None:
    previous = sys.gettrace()
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 1
    )
    assert update_trust._interruptible_tuf_call(deadline, lambda: "ok") == "ok"
    assert sys.gettrace() is previous
    with pytest.raises(RuntimeError, match="injected"):
        update_trust._interruptible_tuf_call(
            deadline, lambda: (_ for _ in ()).throw(RuntimeError("injected"))
        )
    assert sys.gettrace() is previous


def test_tuf_deadline_guards_are_isolated_between_concurrent_threads() -> None:
    barrier = threading.Barrier(2)
    active: list[tuple[int, bool]] = []
    restored: list[tuple[int, bool]] = []
    results: list[tuple[int, int]] = []
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        def prior_trace(frame, event, argument):
            return prior_trace

        sys.settrace(prior_trace)
        try:
            deadline = MonotonicDeadline(
                datetime.now(UTC) + timedelta(seconds=2),
                time.monotonic() + 2,
            )

            def operation() -> int:
                active.append((index, sys.gettrace() is not prior_trace))
                barrier.wait(timeout=1)
                return index

            result = update_trust._interruptible_tuf_call(deadline, operation)
            restored.append((index, sys.gettrace() is prior_trace))
            results.append((index, result))
        except BaseException as error:  # noqa: BLE001 - report worker failures
            errors.append(error)
        finally:
            sys.settrace(None)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(active) == [(0, True), (1, True)]
    assert sorted(restored) == [(0, True), (1, True)]
    assert sorted(results) == [(0, 0), (1, 1)]


def test_tuf_marker_replace_is_parent_fsynced_before_elapsed_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "metadata"
    root.mkdir(mode=0o700)
    marker = root / ".bootstrap-established"
    clock = [0.0]
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])
    original_replace = update_trust.os.replace
    original_fsync = update_trust.os.fsync
    parent_identity = (root.stat().st_dev, root.stat().st_ino)
    parent_synced = False

    def replace_then_expire(source, destination, *args, **kwargs):
        original_replace(source, destination, *args, **kwargs)
        clock[0] = 11.0

    def record_fsync(fd):
        nonlocal parent_synced
        metadata = os.fstat(fd)
        if (metadata.st_dev, metadata.st_ino) == parent_identity:
            parent_synced = True
        return original_fsync(fd)

    monkeypatch.setattr(update_trust.os, "replace", replace_then_expire)
    monkeypatch.setattr(update_trust.os, "fsync", record_fsync)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=10), 10.0
    )

    with pytest.raises(TUFTrustError, match="deadline"):
        update_trust._write_marker(marker, "a" * 64, deadline)

    assert parent_synced
    assert update_trust._marker_root_digest(
        marker,
        MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 20.0),
    ) == "a" * 64


def test_tuf_marker_replace_fsyncs_held_original_parent_after_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "metadata"
    moved = tmp_path / "metadata-moved"
    root.mkdir(mode=0o700)
    marker = root / ".bootstrap-established"
    original_identity = (root.stat().st_dev, root.stat().st_ino)
    original_replace = update_trust.os.replace
    original_fsync = update_trust.os.fsync
    synced: list[tuple[int, int]] = []
    swapped = False

    def replace_then_swap(source, destination, *args, **kwargs):
        nonlocal swapped
        original_replace(source, destination, *args, **kwargs)
        if not swapped and str(destination).endswith(".bootstrap-established"):
            swapped = True
            os.rename(root, moved)
            root.mkdir(mode=0o700)

    def record_fsync(fd):
        metadata = os.fstat(fd)
        if stat.S_ISDIR(metadata.st_mode):
            synced.append((metadata.st_dev, metadata.st_ino))
        return original_fsync(fd)

    monkeypatch.setattr(update_trust.os, "replace", replace_then_swap)
    monkeypatch.setattr(update_trust.os, "fsync", record_fsync)
    update_trust._write_marker(marker, "a" * 64)

    replacement_identity = (root.stat().st_dev, root.stat().st_ino)
    assert original_identity in synced
    assert replacement_identity not in synced
    assert (moved / marker.name).is_file()


def test_tuf_bootstrap_marker_and_authorization_require_successful_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    metadata = tmp_path / "metadata"
    trust = TUFReleaseTrust(
        metadata, tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    original = update_trust._fsync_cache
    with monkeypatch.context() as patcher:
        patcher.setattr(
            update_trust, "_fsync_cache",
            lambda root, deadline: (_ for _ in ()).throw(
                OSError("injected fsync")
            ),
        )
        with pytest.raises(TUFTrustError):
            trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    assert not (metadata / ".bootstrap-established").exists()

    calls = 0
    with monkeypatch.context() as patcher:
        def fail_final(root, deadline):
            nonlocal calls
            calls += 1
            original(root, deadline)
            if calls == 2:
                raise OSError("injected final fsync")

        patcher.setattr(update_trust, "_fsync_cache", fail_final)
        with pytest.raises(TUFTrustError):
            trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    assert (metadata / ".bootstrap-established").is_file()
    assert trust.authorize(
        request, datetime.now(UTC) + timedelta(seconds=2)
    ).target_digest == "2" * 64


def test_tuf_recovers_stale_marker_temp_and_missing_root_pointer(tmp_path: Path) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    metadata = tmp_path / "metadata"
    trust = TUFReleaseTrust(
        metadata, tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    stale = metadata / ".bootstrap-established.new"
    stale.write_text("interrupted")
    stale.chmod(0o600)
    (metadata / "root.json").unlink()

    recovered = trust.authorize(
        request, datetime.now(UTC) + timedelta(seconds=2)
    )

    assert recovered.target_digest == "2" * 64
    assert not stale.exists()
    assert (metadata / "root.json").is_symlink()


def test_tuf_never_bootstrap_rolls_back_when_established_rotated_root_is_lost(
    tmp_path: Path,
) -> None:
    root_bytes, fetcher = _rotated_repository(False)
    metadata = tmp_path / "metadata"
    trust = TUFReleaseTrust(
        metadata, tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))
    root_target = os.readlink(metadata / "root.json")
    assert root_target == "root_history/2.root.json"
    (metadata / "root.json").unlink()
    (metadata / root_target).unlink()
    fetcher.urls.clear()

    with pytest.raises(TUFTrustError, match="operator recovery"):
        trust.authorize(request, datetime.now(UTC) + timedelta(seconds=2))

    assert fetcher.urls == []
    assert (metadata / ".bootstrap-established").is_file()


def test_bounded_https_fetcher_accepts_only_exact_tuf_routes_and_deadline() -> None:
    class Response:
        status = 200

        def __init__(self):
            self.parts = [b"signed", b""]

        def read(self, amount):
            return self.parts.pop(0)

        def release_conn(self):
            pass

    class Pool:
        def request(self, *args, **kwargs):
            return Response()

    fetcher = BoundedHTTPSFetcher(
        "https://control.test.example", ssl.create_default_context(), pool=Pool()
    )
    fetcher.set_deadline(time.monotonic() + 1)
    assert fetcher.download_bytes(
        "https://control.test.example/agent/v1/tuf/metadata/timestamp.json", 64
    ) == b"signed"
    assert fetcher.download_bytes(
        "https://control.test.example/agent/v1/tuf/targets/platform/releases/"
        + "1.2.3/"
        + "a" * 64
        + ".json",
        64,
    ) == b"signed"
    for url in (
        "https://attacker.test/agent/v1/tuf/metadata/timestamp.json",
        "https://control.test.example/agent/v1/tuf/metadata/../secret",
        "https://control.test.example/agent/v1/tuf/targets/a?tag=latest",
        "https://control.test.example/agent/v1/tuf/targets/platform/releases/1.2.3/../escape.json",
        "https://control.test.example/agent/v1/tuf/targets/platform/releases/latest/"
        + "a" * 64
        + ".json",
    ):
        with pytest.raises(DownloadError):
            fetcher.download_bytes(url, 64)


def test_bounded_https_fetcher_observes_deadline_extension_after_first_fetch() -> None:
    class Response:
        status = 200

        def __init__(self):
            self.parts = [b"signed", b""]

        def read(self, amount):
            return self.parts.pop(0)

        def release_conn(self):
            pass

    class Pool:
        def __init__(self) -> None:
            self.totals: list[float] = []

        def request(self, *args, **kwargs):
            self.totals.append(kwargs["timeout"].total)
            return Response()

    pool = Pool()
    fetcher = BoundedHTTPSFetcher(
        "https://control.test.example", ssl.create_default_context(), pool=pool
    )
    lease = MonotonicDeadline.bind(datetime.now(UTC) + timedelta(seconds=0.2))
    fetcher.set_deadline(lease)
    route = "https://control.test.example/agent/v1/tuf/metadata/timestamp.json"

    assert fetcher.download_bytes(route, 64) == b"signed"
    lease.extend(datetime.now(UTC) + timedelta(seconds=1))
    assert fetcher.download_bytes(route, 64) == b"signed"

    assert pool.totals[1] > pool.totals[0] + 0.5


def _oras_policy(tmp_path: Path) -> tuple[ORASPolicy, Path]:
    record = tmp_path / "oras-record.json"
    executable = tmp_path / "oras"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "fd_args = [sys.argv[sys.argv.index(flag) + 1] for flag in ('--registry-config', '--ca-file', '--cert-file', '--key-file')]\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps({{'argv': sys.argv, 'env': dict(os.environ), 'credentials': [pathlib.Path(path).read_text() for path in fd_args]}}))\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "member = output / 'bin/runtime-adapter'\n"
        "member.parent.mkdir(parents=True, exist_ok=True)\n"
        "member.write_bytes(b'x' * 17)\n"
        "member.chmod(0o500)\n"
    )
    executable.chmod(0o755)
    files = {}
    for name, mode in (("auth.json", 0o600), ("ca.pem", 0o644), ("client.pem", 0o644), ("client.key", 0o600)):
        path = tmp_path / name
        path.write_text(name)
        path.chmod(mode)
        files[name] = path
    policy = ORASPolicy(
        registry_origin="https://registry.test.example",
        repository="vonk/releases",
        executable=executable,
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        executable_version="1.3.3",
        auth_path=files["auth.json"],
        ca_path=files["ca.pem"],
        client_certificate_path=files["client.pem"],
        client_key_path=files["client.key"],
        allow_unprivileged_test_files=True,
    )
    return policy, record


def test_oras_uses_only_digest_reference_fixed_files_and_fixed_environment(tmp_path: Path) -> None:
    policy, record = _oras_policy(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    descriptor = ReleaseDescriptor.parse(_descriptor())

    ORASClient(policy).pull(
        descriptor, staging, datetime.now(UTC) + timedelta(seconds=2)
    )

    invocation = json.loads(record.read_text())
    assert invocation["argv"][0].startswith("/proc/self/fd/")
    assert invocation["argv"][1:] == [
        "pull",
        "registry.test.example/vonk/releases@sha256:" + "1" * 64,
        "--output",
        str(staging),
        "--registry-config",
        invocation["argv"][6],
        "--ca-file",
        invocation["argv"][8],
        "--cert-file",
        invocation["argv"][10],
        "--key-file",
        invocation["argv"][12],
        "--concurrency",
        "2",
    ]
    assert set(invocation["env"]) == {
        "LANG", "LC_ALL", "PATH", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE"
    }
    assert "HTTP_PROXY" not in invocation["env"]
    for value in invocation["argv"][6:13:2]:
        assert value.startswith("/proc/self/fd/")
    assert invocation["credentials"] == [
        "auth.json", "ca.pem", "client.pem", "client.key"
    ]


def test_production_private_oras_file_accepts_only_service_uid_exact_0600(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_uid = 42424
    metadata = type("Metadata", (), {
        "st_mode": stat.S_IFREG | 0o600,
        "st_nlink": 1,
        "st_uid": service_uid,
    })()
    monkeypatch.setattr(oci_module.os, "geteuid", lambda: service_uid)

    oci_module._trusted_policy_file(
        metadata, private=True, allow_unprivileged_test_files=False
    )
    with pytest.raises(OCIError):
        oci_module._trusted_policy_file(
            metadata, private=False, allow_unprivileged_test_files=False
        )
    for mode in (0o400, 0o640, 0o600 | stat.S_ISUID):
        changed = type("Metadata", (), {
            "st_mode": stat.S_IFREG | mode,
            "st_nlink": 1,
            "st_uid": service_uid,
        })()
        with pytest.raises(OCIError):
            oci_module._trusted_policy_file(
                changed, private=True, allow_unprivileged_test_files=False
            )


def test_production_private_oras_snapshot_opens_service_file_below_root_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = tmp_path / "auth.json"
    auth.write_bytes(b'{"auths":{}}')
    auth.chmod(0o600)
    original_fstat = oci_module.os.fstat

    def deployed_metadata(fd):
        metadata = original_fstat(fd)
        if stat.S_ISDIR(metadata.st_mode):
            return type("DirectoryMetadata", (), {
                "st_mode": stat.S_IFDIR | 0o755,
                "st_uid": 0,
            })()
        return metadata

    monkeypatch.setattr(oci_module.os, "fstat", deployed_metadata)
    snapshot = oci_module._snapshot_policy_file(
        auth, private=True, allow_unprivileged_test_files=False
    )
    try:
        assert os.read(snapshot, 64) == b'{"auths":{}}'
    finally:
        os.close(snapshot)


def test_oras_uses_sealed_credential_snapshots_after_path_and_inode_mutation(tmp_path: Path) -> None:
    policy, record = _oras_policy(tmp_path)
    client = ORASClient(policy)
    policy.auth_path.unlink()
    policy.auth_path.write_text("attacker replacement")
    policy.auth_path.chmod(0o600)
    policy.ca_path.chmod(0o600)
    policy.ca_path.write_text("same inode mutation")
    policy.ca_path.chmod(0o644)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)

    client.pull(
        ReleaseDescriptor.parse(_descriptor()),
        staging,
        datetime.now(UTC) + timedelta(seconds=2),
    )

    invocation = json.loads(record.read_text())
    assert invocation["credentials"][:2] == ["auth.json", "ca.pem"]


def test_oras_resolves_shared_active_credential_provider_for_every_pull(tmp_path: Path) -> None:
    policy, record = _oras_policy(tmp_path)
    second_certificate = tmp_path / "client-2.pem"
    second_certificate.write_text("rotated-client.pem")
    second_certificate.chmod(0o644)
    second_key = tmp_path / "client-2.key"
    second_key.write_text("rotated-client.key")
    second_key.chmod(0o600)

    class SwitchingProvider:
        certificate = policy.client_certificate_path
        key = policy.client_key_path

        @contextmanager
        def snapshot(self):
            yield CredentialSnapshot(
                ca_path=policy.ca_path,
                certificate_path=self.certificate,
                private_key_path=self.key,
            )

    provider = SwitchingProvider()
    client = ORASClient(replace(policy, credential_provider=provider))
    first_staging = tmp_path / "first-staging"
    first_staging.mkdir(mode=0o700)
    second_staging = tmp_path / "second-staging"
    second_staging.mkdir(mode=0o700)
    descriptor = ReleaseDescriptor.parse(_descriptor())

    client.pull(descriptor, first_staging, datetime.now(UTC) + timedelta(seconds=2))
    first = json.loads(record.read_text())["credentials"]
    provider.certificate = second_certificate
    provider.key = second_key
    client.pull(descriptor, second_staging, datetime.now(UTC) + timedelta(seconds=2))
    second = json.loads(record.read_text())["credentials"]

    assert first == ["auth.json", "ca.pem", "client.pem", "client.key"]
    assert second == [
        "auth.json", "ca.pem", "rotated-client.pem", "rotated-client.key",
    ]


def test_oras_closes_partial_dynamic_credential_snapshot_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, _ = _oras_policy(tmp_path)

    class Provider:
        @contextmanager
        def snapshot(self):
            yield CredentialSnapshot(
                ca_path=policy.ca_path,
                certificate_path=policy.client_certificate_path,
                private_key_path=policy.client_key_path,
            )

    client = ORASClient(replace(policy, credential_provider=Provider()))
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    original = oci_module._snapshot_provider_file
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OCIError("credential snapshot failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(oci_module, "_snapshot_provider_file", fail_second)
    baseline = len(tuple(Path("/proc/self/fd").iterdir()))

    with pytest.raises(OCIError, match="credential snapshot failed"):
        client.pull(
            ReleaseDescriptor.parse(_descriptor()),
            staging,
            datetime.now(UTC) + timedelta(seconds=2),
        )

    assert len(tuple(Path("/proc/self/fd").iterdir())) == baseline


def test_oras_executable_snapshot_stops_after_crossing_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, _ = _oras_policy(tmp_path)
    with policy.executable.open("ab") as executable:
        executable.write(b"#" * (3 * 64 * 1024))
    policy = replace(
        policy,
        executable_sha256=hashlib.sha256(policy.executable.read_bytes()).hexdigest(),
    )
    client = ORASClient(policy)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    original_read = oci_module.os.read
    reads = 0

    def slow_read(fd, size):
        nonlocal reads
        reads += 1
        time.sleep(0.03)
        return original_read(fd, size)

    monkeypatch.setattr(oci_module.os, "read", slow_read)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    with pytest.raises(OCIError, match="deadline"):
        client.pull(ReleaseDescriptor.parse(_descriptor()), staging, deadline)
    assert reads == 1


@pytest.mark.parametrize("phase", ["ancestry", "open", "fstat", "hash-setup"])
def test_oras_snapshot_deadline_stops_before_next_setup_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    executable = tmp_path / "oras"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    expected = hashlib.sha256(executable.read_bytes()).hexdigest()
    expired = [False]
    events: list[str] = []
    original_parent = nvidia_tools._open_parent
    original_open = nvidia_tools.os.open
    original_fstat = nvidia_tools.os.fstat
    original_sha256 = nvidia_tools.hashlib.sha256
    original_memfd = nvidia_tools.os.memfd_create

    def check() -> None:
        if expired[0]:
            raise DeadlineBindingError("deadline has elapsed")

    def parent_then_expire(*args, **kwargs):
        result = original_parent(*args, **kwargs)
        events.append("ancestry")
        if phase == "ancestry":
            expired[0] = True
        return result

    def open_then_expire(path, flags, *args, **kwargs):
        result = original_open(path, flags, *args, **kwargs)
        if path == executable.name and kwargs.get("dir_fd") is not None:
            events.append("open")
            if phase == "open":
                expired[0] = True
        return result

    def fstat_then_expire(fd):
        result = original_fstat(fd)
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            target = ""
        if target == str(executable):
            events.append("fstat")
            if phase == "fstat":
                expired[0] = True
        return result

    def hash_then_expire(*args, **kwargs):
        result = original_sha256(*args, **kwargs)
        events.append("hash-setup")
        if phase == "hash-setup":
            expired[0] = True
        return result

    def record_memfd(name, flags):
        events.append("memfd")
        return original_memfd(name, flags)

    monkeypatch.setattr(nvidia_tools, "_open_parent", parent_then_expire)
    monkeypatch.setattr(nvidia_tools.os, "open", open_then_expire)
    monkeypatch.setattr(nvidia_tools.os, "fstat", fstat_then_expire)
    monkeypatch.setattr(nvidia_tools.hashlib, "sha256", hash_then_expire)
    monkeypatch.setattr(nvidia_tools.os, "memfd_create", record_memfd)

    with pytest.raises(DeadlineBindingError, match="deadline"):
        nvidia_tools.open_verified_executable(
            executable,
            expected,
            _test_only_allow_unprivileged=True,
            _check_deadline=check,
        )

    expected_events = ["ancestry", "open", "fstat", "hash-setup", "memfd"]
    assert events == expected_events[: expected_events.index(phase) + 1]


def test_repeated_oras_ancestry_expiry_closes_all_open_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "nested" / "bin" / "oras"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    expected = hashlib.sha256(executable.read_bytes()).hexdigest()
    original_open = nvidia_tools.os.open
    expired = [False]

    def expire_after_child_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "nested" and kwargs.get("dir_fd") is not None:
            expired[0] = True
        return descriptor

    def check() -> None:
        if expired[0]:
            raise DeadlineBindingError("deadline has elapsed")

    monkeypatch.setattr(nvidia_tools.os, "open", expire_after_child_open)
    baseline = len(tuple(Path("/proc/self/fd").iterdir()))
    for _ in range(32):
        expired[0] = False
        with pytest.raises(DeadlineBindingError, match="deadline"):
            nvidia_tools.open_verified_executable(
                executable,
                expected,
                _test_only_allow_unprivileged=True,
                _check_deadline=check,
            )
        assert len(tuple(Path("/proc/self/fd").iterdir())) == baseline


def test_oras_rejects_policy_mismatch_before_launch(tmp_path: Path) -> None:
    policy, record = _oras_policy(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    changed = _descriptor() | {"repository": "other/releases"}

    with pytest.raises(OCIError):
        ORASClient(policy).pull(
            ReleaseDescriptor.parse(changed),
            staging,
            datetime.now(UTC) + timedelta(seconds=2),
        )
    assert not record.exists()


def test_oras_close_waits_for_active_pull_and_then_fails_closed(tmp_path: Path) -> None:
    policy, _ = _oras_policy(tmp_path)
    client = ORASClient(policy)
    entered = threading.Event()
    release = threading.Event()

    class Runner:
        def run(self, request):
            entered.set()
            release.wait(2)
            return ProcessOutcome(0, b"", b"")

    client._runner = Runner()
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    errors: list[Exception] = []

    def pull():
        try:
            client.pull(
                ReleaseDescriptor.parse(_descriptor()), staging,
                datetime.now(UTC) + timedelta(seconds=2),
            )
        except Exception as error:  # noqa: BLE001 - report thread failures
            errors.append(error)

    pull_thread = threading.Thread(target=pull)
    close_thread = threading.Thread(target=client.close)
    pull_thread.start()
    assert entered.wait(1)
    close_thread.start()
    time.sleep(0.02)
    assert close_thread.is_alive()
    release.set()
    pull_thread.join()
    close_thread.join()
    assert errors == []
    with pytest.raises(OCIError, match="closed"):
        client.pull(
            ReleaseDescriptor.parse(_descriptor()), staging,
            datetime.now(UTC) + timedelta(seconds=1),
        )


def test_release_install_is_atomic_verified_and_idempotent(tmp_path: Path) -> None:
    root_bytes, fetcher = _signed_repository(_descriptor())
    trust = TUFReleaseTrust(
        tmp_path / "metadata", tmp_path / "targets",
        "https://control.test.example/agent/v1/tuf/metadata/",
        "https://control.test.example/agent/v1/tuf/targets/",
        root_bytes, fetcher,
        "https://registry.test.example", "vonk/releases", "linux-arm64",
    )
    policy, record = _oras_policy(tmp_path)
    releases_root = tmp_path / "release-store"
    staging_root = tmp_path / "release-staging"
    installer = ReleaseInstaller(
        trust, ORASClient(policy), releases_root, staging_root
    )
    request = ReleaseRequest.parse(VALID_RELEASE)

    first = installer.install(request, datetime.now(UTC) + timedelta(seconds=2))
    first_invocation = record.read_bytes()
    second = installer.install(request, datetime.now(UTC) + timedelta(seconds=2))

    installed = releases_root / ("2" * 64)
    assert first.status == "installed"
    assert second.status == "already-installed"
    assert (installed / "bin/runtime-adapter").read_bytes() == b"x" * 17
    assert (installed / ".install-receipt.json").is_file()
    assert record.read_bytes() == first_invocation
    assert {entry.name for entry in staging_root.iterdir()} == {
        release_module._INSTALL_RECOVERY_LOCK_NAME
    }


def test_installed_verification_rejects_destination_swap_between_receipt_and_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())
    destination = tmp_path / "installed"
    replacement = tmp_path / "replacement"
    moved = tmp_path / "moved"
    for root in (destination, replacement):
        member = root / "bin/runtime-adapter"
        member.parent.mkdir(parents=True, mode=0o700)
        member.write_bytes(b"x" * 17)
        member.chmod(0o500)
        release_module._write_receipt(root, descriptor)

    original_loads = release_module.json.loads
    swapped = False

    def loads_then_swap(raw, **kwargs):
        nonlocal swapped
        document = original_loads(raw, **kwargs)
        if not swapped:
            swapped = True
            os.rename(destination, moved)
            os.rename(replacement, destination)
        return document

    monkeypatch.setattr(release_module.json, "loads", loads_then_swap)

    with pytest.raises(ReleaseInstallError, match="identity"):
        release_module._verify_installed(
            destination.parent, destination.name, descriptor
        )

    assert destination.stat().st_ino != moved.stat().st_ino


@pytest.mark.parametrize("mutation", ["reordered", "duplicate", "trailing"])
def test_installed_receipt_requires_duplicate_free_canonical_bytes(
    tmp_path: Path, mutation: str
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())
    root = tmp_path / "installed"
    member = root / "bin/runtime-adapter"
    member.parent.mkdir(parents=True, mode=0o700)
    member.write_bytes(b"x" * 17)
    member.chmod(0o500)
    release_module._write_receipt(root, descriptor)
    receipt = root / ".install-receipt.json"
    canonical = release_module._receipt_bytes(descriptor)
    assert verify_installed_release(root) == descriptor

    if mutation == "reordered":
        raw = json.dumps(
            {"release": descriptor.to_mapping(), "schema_version": 1},
            indent=2,
        ).encode() + b"\n"
    elif mutation == "duplicate":
        raw = (
            b'{"schema_version":1,"schema_version":1,"release":'
            + json.dumps(
                descriptor.to_mapping(), sort_keys=True, separators=(",", ":")
            ).encode()
            + b"}\n"
        )
    else:
        raw = canonical + b"\n"
    receipt.chmod(0o600)
    receipt.write_bytes(raw)
    receipt.chmod(0o400)

    with pytest.raises(ReleaseInstallError, match="receipt"):
        verify_installed_release(root)


def test_release_member_deadline_stops_after_one_blocking_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"x" * (3 * 64 * 1024)
    document = _descriptor()
    document["target_length"] = len(content)
    document["members"][0]["size"] = len(content)
    document["members"][0]["sha256"] = hashlib.sha256(content).hexdigest()
    descriptor = ReleaseDescriptor.parse(document)
    root = tmp_path / "tree"
    member = root / "bin/runtime-adapter"
    member.parent.mkdir(parents=True, mode=0o700)
    member.write_bytes(content)
    member.chmod(0o500)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    original_read = release_module.os.read
    reads = 0

    def slow_read(fd, size):
        nonlocal reads
        reads += 1
        time.sleep(0.03)
        return original_read(fd, size)

    monkeypatch.setattr(release_module.os, "read", slow_read)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    try:
        with pytest.raises(ReleaseInstallError, match="deadline"):
            release_module._verify_release_tree_fd(
                root_fd, descriptor, deadline=deadline
            )
    finally:
        os.close(root_fd)
    assert reads == 1


def test_release_recursive_fsync_deadline_stops_after_crossing_syscall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    for name in ("a/one", "b/two", "c/three"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    fsyncs = 0

    def slow_fsync(fd):
        nonlocal fsyncs
        fsyncs += 1
        time.sleep(0.03)

    monkeypatch.setattr(release_module.os, "fsync", slow_fsync)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    try:
        with pytest.raises(ReleaseInstallError, match="deadline"):
            release_module._fsync_tree_fd(root_fd, deadline)
    finally:
        os.close(root_fd)
    assert fsyncs == 1


def test_release_rename_is_parent_fsynced_before_elapsed_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    original_rename = release_module._rename_noreplace
    original_fsync = release_module.os.fsync
    clock = [0.0]
    parent_synced = False
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])

    def rename_then_expire(*args):
        original_rename(*args)
        if args[3] == "2" * 64:
            clock[0] = 11.0

    def record_fsync(fd):
        nonlocal parent_synced
        metadata = os.fstat(fd)
        if releases.exists():
            root_metadata = releases.stat()
            if (metadata.st_dev, metadata.st_ino) == (
                root_metadata.st_dev, root_metadata.st_ino
            ):
                parent_synced = True
        return original_fsync(fd)

    monkeypatch.setattr(release_module, "_rename_noreplace", rename_then_expire)
    monkeypatch.setattr(release_module.os, "fsync", record_fsync)
    request = ReleaseRequest.parse(VALID_RELEASE)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=10), 10.0
    )

    with pytest.raises(ReleaseInstallError, match="deadline"):
        installer.install(request, deadline)

    assert parent_synced
    assert (releases / ("2" * 64)).is_dir()
    clock[0] = 0.0
    assert installer.install(
        request,
        MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 10.0),
    ).status == "already-installed"


def test_release_publication_fsyncs_held_original_parent_after_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    moved = tmp_path / "releases-moved"
    staging = tmp_path / "staging"
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    original_rename = release_module._rename_noreplace
    original_fsync = release_module.os.fsync
    synced: list[tuple[int, int]] = []

    def rename_then_swap(*args):
        original_rename(*args)
        if args[3] == "2" * 64:
            os.rename(releases, moved)
            releases.mkdir(mode=0o700)

    def record_fsync(fd):
        metadata = os.fstat(fd)
        if stat.S_ISDIR(metadata.st_mode):
            synced.append((metadata.st_dev, metadata.st_ino))
        return original_fsync(fd)

    monkeypatch.setattr(release_module, "_rename_noreplace", rename_then_swap)
    monkeypatch.setattr(release_module.os, "fsync", record_fsync)
    with pytest.raises(ReleaseInstallError):
        installer.install(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )

    original_identity = (moved.stat().st_dev, moved.stat().st_ino)
    replacement_identity = (releases.stat().st_dev, releases.stat().st_ino)
    assert original_identity in synced
    assert replacement_identity not in synced
    assert (moved / ("2" * 64)).is_dir()


@pytest.mark.parametrize("phase", ["create", "chmod", "open"])
def test_expired_staging_setup_defers_cleanup_and_next_attempt_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    clock = [0.0]
    armed = [True]
    mutations: list[str] = []
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])
    original_mkdir = release_module.os.mkdir
    original_chmod = release_module.os.chmod
    original_fchmod = release_module.os.fchmod
    original_open = release_module.os.open
    original_unlink = release_module.os.unlink
    original_rmdir = release_module.os.rmdir

    def expire():
        if armed[0]:
            clock[0] = 11.0
            armed[0] = False

    def mkdir(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if phase == "create" and str(path).startswith(".install-"):
            expire()
        return result

    def chmod(path, mode, *args, **kwargs):
        result = original_chmod(path, mode, *args, **kwargs)
        if phase == "chmod" and ".install-" in str(path):
            expire()
        return result

    def fchmod(fd, mode):
        result = original_fchmod(fd, mode)
        if phase == "chmod":
            expire()
        return result

    def open_then_expire(path, flags, *args, **kwargs):
        result = original_open(path, flags, *args, **kwargs)
        if (
            phase == "open"
            and ".install-" in str(path)
            and not str(path).endswith(".lock")
        ):
            expire()
        return result

    def unlink(path, *args, **kwargs):
        if clock[0] > 10:
            mutations.append("unlink")
        return original_unlink(path, *args, **kwargs)

    def rmdir(path, *args, **kwargs):
        if clock[0] > 10:
            mutations.append("rmdir")
        return original_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(release_module.os, "mkdir", mkdir)
    monkeypatch.setattr(release_module.os, "chmod", chmod)
    monkeypatch.setattr(release_module.os, "fchmod", fchmod)
    monkeypatch.setattr(release_module.os, "open", open_then_expire)
    monkeypatch.setattr(release_module.os, "unlink", unlink)
    monkeypatch.setattr(release_module.os, "rmdir", rmdir)
    request = ReleaseRequest.parse(VALID_RELEASE)

    with pytest.raises(ReleaseInstallError, match="deadline"):
        installer.install(
            request,
            MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 10.0),
        )
    assert mutations == []
    assert sum(
        release_module._STAGING_NAME.fullmatch(entry.name) is not None
        for entry in staging.iterdir()
    ) == 1

    clock[0] = 0.0
    assert installer.install(
        request,
        MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 10.0),
    ).status == "installed"
    assert not any(
        release_module._STAGING_NAME.fullmatch(entry.name)
        for entry in staging.iterdir()
    )
    assert tuple(staging.glob(".recovery-*")) == ()
    assert len(tuple(staging.glob(".quarantine-*"))) == (
        1 if phase in {"create", "open"} else 0
    )


@pytest.mark.parametrize(
    "phase",
    [
        "intent-open",
        "intent-write",
        "intent-fsync",
        "intent-fstat",
        "intent-parent-fsync",
        "mkdir",
        "staging-open",
        "staging-fstat",
        "temp-open",
        "temp-write",
        "temp-fsync",
        "temp-fstat",
        "temp-parent-fsync",
        "replace",
        "replace-parent-fsync",
        "complete-stat",
        "fchmod",
    ],
)
def test_staging_ownership_transaction_stops_after_one_crossing_syscall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        called = False

        def pull(self, descriptor, destination, deadline):
            self.called = True

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    clock = [0.0]
    events: list[str] = []
    parent_fsyncs = 0
    original_open = release_module.os.open
    original_write = release_module.os.write
    original_fsync = release_module.os.fsync
    original_fstat = release_module.os.fstat
    original_mkdir = release_module.os.mkdir
    original_replace = release_module.os.replace
    original_rename_noreplace = release_module._rename_noreplace
    original_stat = release_module.os.stat
    original_fchmod = release_module.os.fchmod
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])

    def record(event: str) -> None:
        events.append(event)
        if event == phase:
            clock[0] = 11.0

    def target_for_fd(fd: int) -> str:
        try:
            return os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            return ""

    def tracked_open(path, flags, *args, **kwargs):
        result = original_open(path, flags, *args, **kwargs)
        value = str(path)
        if value.endswith(".state"):
            record("intent-open")
        elif value.endswith(".new"):
            record("temp-open")
        elif value.startswith(".install-") and not value.endswith(".lock"):
            record("staging-open")
        return result

    def tracked_write(fd, data):
        result = original_write(fd, data)
        target = target_for_fd(fd)
        if target.endswith(".state"):
            record("intent-write")
        elif target.endswith(".new"):
            record("temp-write")
        return result

    def tracked_fsync(fd):
        nonlocal parent_fsyncs
        result = original_fsync(fd)
        target = target_for_fd(fd)
        if target.endswith(".state"):
            record("intent-fsync")
        elif target.endswith(".new"):
            record("temp-fsync")
        elif target == str(staging):
            parent_fsyncs += 1
            record(
                {
                    1: "intent-parent-fsync",
                    2: "temp-parent-fsync",
                    3: "replace-parent-fsync",
                }.get(parent_fsyncs, f"parent-fsync-{parent_fsyncs}")
            )
        return result

    def tracked_fstat(fd):
        result = original_fstat(fd)
        target = target_for_fd(fd)
        if target.endswith(".state"):
            record("intent-fstat")
        elif target.endswith(".new"):
            record("temp-fstat")
        elif "/.install-" in target and not target.endswith(".lock"):
            record("staging-fstat")
        return result

    def tracked_mkdir(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if str(path).startswith(".install-"):
            record("mkdir")
        return result

    def tracked_replace(source, destination, *args, **kwargs):
        result = original_replace(source, destination, *args, **kwargs)
        if str(source).endswith(".new") and str(destination).endswith(".state"):
            record("replace")
        return result

    def tracked_rename_noreplace(source_fd, source, destination_fd, destination):
        result = original_rename_noreplace(
            source_fd, source, destination_fd, destination
        )
        if str(source).endswith(".new") and str(destination).endswith(".state"):
            record("replace")
        return result

    def tracked_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if str(path).endswith(".state") and kwargs.get("dir_fd") is not None:
            record("complete-stat")
        return result

    def tracked_fchmod(fd, mode):
        result = original_fchmod(fd, mode)
        if "/.install-" in target_for_fd(fd):
            record("fchmod")
        return result

    monkeypatch.setattr(release_module.os, "open", tracked_open)
    monkeypatch.setattr(release_module.os, "write", tracked_write)
    monkeypatch.setattr(release_module.os, "fsync", tracked_fsync)
    monkeypatch.setattr(release_module.os, "fstat", tracked_fstat)
    monkeypatch.setattr(release_module.os, "mkdir", tracked_mkdir)
    monkeypatch.setattr(release_module.os, "replace", tracked_replace)
    monkeypatch.setattr(
        release_module, "_rename_noreplace", tracked_rename_noreplace
    )
    monkeypatch.setattr(release_module.os, "stat", tracked_stat)
    monkeypatch.setattr(release_module.os, "fchmod", tracked_fchmod)
    transport = Transport()

    with pytest.raises(ReleaseInstallError, match="deadline"):
        ReleaseInstaller(Trust(), transport, releases, staging).install(
            ReleaseRequest.parse(VALID_RELEASE),
            MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 10.0),
        )

    assert events[-1] == phase
    assert not transport.called


def test_expired_member_verification_never_recursively_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"x" * (3 * 64 * 1024)
    document = _descriptor()
    document["target_length"] = len(content)
    document["members"][0]["size"] = len(content)
    document["members"][0]["sha256"] = hashlib.sha256(content).hexdigest()
    descriptor = ReleaseDescriptor.parse(document)

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(content)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    clock = [0.0]
    armed = [True]
    mutations: list[str] = []
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])
    original_read = release_module.os.read
    original_unlink = release_module.os.unlink
    original_rmdir = release_module.os.rmdir

    def expire_after_read(fd, size):
        data = original_read(fd, size)
        if armed[0]:
            armed[0] = False
            clock[0] = 11.0
        return data

    def unlink(path, *args, **kwargs):
        if clock[0] > 10:
            mutations.append("unlink")
        return original_unlink(path, *args, **kwargs)

    def rmdir(path, *args, **kwargs):
        if clock[0] > 10:
            mutations.append("rmdir")
        return original_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(release_module.os, "read", expire_after_read)
    monkeypatch.setattr(release_module.os, "unlink", unlink)
    monkeypatch.setattr(release_module.os, "rmdir", rmdir)
    request = ReleaseRequest.parse(VALID_RELEASE)
    with pytest.raises(ReleaseInstallError, match="deadline"):
        installer.install(
            request,
            MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 10.0),
        )
    assert mutations == []
    assert sum(
        release_module._STAGING_NAME.fullmatch(entry.name) is not None
        for entry in staging.iterdir()
    ) == 1

    clock[0] = 0.0
    assert installer.install(
        request,
        MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 10.0),
    ).status == "installed"
    assert {entry.name for entry in staging.iterdir()} == {
        release_module._INSTALL_RECOVERY_LOCK_NAME
    }


def test_deferred_reaper_preserves_foreign_inode_substituted_at_owned_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    clock = [0.0]
    armed = [True]
    original_fchmod = release_module.os.fchmod
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])

    def expire_after_identity(fd, mode):
        result = original_fchmod(fd, mode)
        if armed[0]:
            armed[0] = False
            clock[0] = 11.0
        return result

    monkeypatch.setattr(release_module.os, "fchmod", expire_after_identity)
    request = ReleaseRequest.parse(VALID_RELEASE)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=10), 10.0
    )
    with pytest.raises(ReleaseInstallError, match="deadline"):
        installer.install(request, deadline)

    owned = next(
        entry
        for entry in staging.iterdir()
        if release_module._STAGING_NAME.fullmatch(entry.name)
    )
    moved = staging / ".attacker-moved-owned-tree"
    os.rename(owned, moved)
    owned.mkdir(mode=0o700)
    foreign = owned / "foreign"
    foreign.write_bytes(b"preserve")

    clock[0] = 0.0
    restarted = ReleaseInstaller(Trust(), Transport(), releases, staging)
    assert restarted.install(request, deadline).status == "installed"
    assert foreign.read_bytes() == b"preserve"
    assert moved.is_dir()
    assert restarted._deferred_staging == {}


def test_fresh_inspection_reaps_authenticated_partial_staging_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    clock = [0.0]
    original_fchmod = release_module.os.fchmod
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])

    def expire_after_identity(fd, mode):
        result = original_fchmod(fd, mode)
        clock[0] = 11.0
        return result

    monkeypatch.setattr(release_module.os, "fchmod", expire_after_identity)
    request = ReleaseRequest.parse(VALID_RELEASE)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=10), 10.0
    )
    with pytest.raises(ReleaseInstallError, match="deadline"):
        ReleaseInstaller(Trust(), Transport(), releases, staging).install(
            request, deadline
        )
    assert tuple(staging.glob(".recovery-*.state"))

    clock[0] = 0.0
    restarted = ReleaseInstaller(Trust(), Transport(), releases, staging)
    assert restarted.inspect(request, deadline).disposition is ReleaseDisposition.SAFE_TO_RESUME
    assert {entry.name for entry in staging.iterdir()} == {
        release_module._INSTALL_RECOVERY_LOCK_NAME
    }


def test_recovery_record_swap_before_delete_preserves_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    token = "c" * 16
    staging_name = f".install-{'2' * 64}-{token}"
    recovery_name = f".recovery-{token}.state"
    staging_path = staging / staging_name
    staging_path.mkdir(mode=0o700)
    metadata = staging_path.stat()
    identity = (metadata.st_dev, metadata.st_ino)
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    original_rename = release_module._rename_noreplace
    moved = staging / ".moved-original-record"
    swapped = [False]
    try:
        record_identity = release_module._write_recovery_record_fd(
            parent_fd, recovery_name, staging_name, identity
        )
        record = staging / recovery_name
        canonical = record.read_bytes()

        def swap_then_rename(source_fd, source, destination_fd, destination):
            if source == recovery_name and not swapped[0]:
                swapped[0] = True
                os.rename(record, moved)
                record.write_bytes(canonical)
                record.chmod(0o600)
            return original_rename(
                source_fd, source, destination_fd, destination
            )

        monkeypatch.setattr(release_module, "_rename_noreplace", swap_then_rename)
        assert not release_module._remove_recovery_record_fd(
            parent_fd, recovery_name, record_identity, lambda: None
        )
    finally:
        os.close(parent_fd)

    assert moved.is_file()
    assert (staging / recovery_name).is_file()
    assert (staging / recovery_name).stat().st_ino != moved.stat().st_ino


def test_recursive_cleanup_final_remove_preserves_empty_foreign_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    name = ".install-owned"
    owned = staging / name
    owned.mkdir(mode=0o700)
    metadata = owned.stat()
    identity = (metadata.st_dev, metadata.st_ino)
    moved = staging / ".moved-owned"
    original_rename = release_module._rename_noreplace
    swapped = [False]

    def substitute_then_rename(source_fd, source, destination_fd, destination):
        if source == name and not swapped[0]:
            swapped[0] = True
            os.rename(owned, moved)
            owned.mkdir(mode=0o700)
        return original_rename(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(release_module, "_rename_noreplace", substitute_then_rename)
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        release_module._remove_bound_tree_fd(
            parent_fd, name, identity, lambda: None
        )
    finally:
        os.close(parent_fd)

    assert moved.is_dir()
    assert owned.is_dir()
    assert moved.stat().st_ino != owned.stat().st_ino


def test_restart_resumes_tree_quarantined_at_final_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    token = "a" * 16
    name = f".install-{'2' * 64}-{token}"
    owned = staging / name
    owned.mkdir(mode=0o700)
    metadata = owned.stat()
    identity = (metadata.st_dev, metadata.st_ino)
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    original_rename = release_module._rename_noreplace

    def crash_after_quarantine(source_fd, source, destination_fd, destination):
        original_rename(source_fd, source, destination_fd, destination)
        if source == name:
            raise TimeoutError("crash after final quarantine")

    monkeypatch.setattr(
        release_module, "_rename_noreplace", crash_after_quarantine
    )
    try:
        with pytest.raises(TimeoutError, match="final quarantine"):
            release_module._remove_bound_tree_fd(
                parent_fd, name, identity, lambda: None
            )
        monkeypatch.setattr(
            release_module, "_rename_noreplace", original_rename
        )
        release_module._remove_bound_tree_fd(
            parent_fd, name, identity, lambda: None
        )
    finally:
        os.close(parent_fd)

    assert tuple(staging.iterdir()) == ()


def test_fresh_reaper_resolves_recovery_record_quarantine_without_growth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    original_rename = release_module._rename_noreplace
    try:
        for index in range(3):
            token = f"{index + 11:016x}"
            staging_name = f".install-{'2' * 64}-{token}"
            record_name = f".recovery-{token}.state"
            record_identity = release_module._write_recovery_intent_fd(
                parent_fd, record_name, staging_name
            )

            def crash_after_quarantine(
                source_fd,
                source,
                destination_fd,
                destination,
                expected_record=record_name,
            ):
                original_rename(source_fd, source, destination_fd, destination)
                if source == expected_record:
                    raise TimeoutError("crash after record quarantine")

            monkeypatch.setattr(
                release_module, "_rename_noreplace", crash_after_quarantine
            )
            with pytest.raises(TimeoutError, match="record quarantine"):
                release_module._remove_recovery_record_fd(
                    parent_fd, record_name, record_identity, lambda: None
                )
            monkeypatch.setattr(
                release_module, "_rename_noreplace", original_rename
            )
            ReleaseInstaller(
                object(), object(), tmp_path / "releases", staging
            )._reap_deferred_staging(parent_fd)
            assert tuple(staging.iterdir()) == ()
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize("damage", ["corrupt", "symlink"])
def test_reaper_preserves_unsafe_recovery_record_quarantine(
    tmp_path: Path, damage: str
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    token = "c" * 16
    staging_name = f".install-{'2' * 64}-{token}"
    quarantine_name = f".quarantine-recovery-{token}-{'d' * 16}"
    quarantine = staging / quarantine_name
    if damage == "corrupt":
        quarantine.write_bytes(b"not canonical\n")
        quarantine.chmod(0o600)
    else:
        foreign = tmp_path / "foreign-quarantine"
        foreign.write_bytes(release_module._recovery_intent_bytes(staging_name))
        quarantine.symlink_to(foreign)
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ReleaseInstallError, match="recovery record"):
            ReleaseInstaller(
                object(), object(), tmp_path / "releases", staging
            )._reap_deferred_staging(parent_fd)
    finally:
        os.close(parent_fd)
    assert os.path.lexists(quarantine)


def test_recovery_backlog_bound_is_aggregate_across_artifact_categories(
    tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    for index in range(6):
        token = f"{index + 1:016x}"
        (staging / f".recovery-{token}.state").write_bytes(b"")
        (staging / f".recovery-{token}.state").chmod(0o600)
    for index in range(6, 11):
        token = f"{index + 1:016x}"
        (staging / f".recovery-{token}.new").write_bytes(b"")
        (staging / f".recovery-{token}.new").chmod(0o600)
    for index in range(11, 17):
        token = f"{index + 1:016x}"
        (staging / f".install-{'2' * 64}-{token}").mkdir(mode=0o700)
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ReleaseInstallError, match="backlog"):
            release_module._read_recovery_records_fd(parent_fd, lambda: None)
    finally:
        os.close(parent_fd)


def test_recovery_backlog_bound_includes_active_reservations(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    for index in range(15):
        (staging / f".unsafe-recovery-{index:016x}").write_bytes(b"held")
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ReleaseInstallError, match="backlog"):
            release_module._read_recovery_records_fd(
                parent_fd, lambda: None, active_reservations=2
            )
    finally:
        os.close(parent_fd)


def test_deferred_staging_backlog_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = ReleaseInstaller(
        object(), object(), tmp_path / "releases", tmp_path / "staging"
    )
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    installer._deferred_staging = {
        f".install-owned-{index}": (1, index)
        for index in range(release_module._MAX_DEFERRED_STAGING)
    }
    monkeypatch.setattr(
        release_module,
        "_remove_bound_tree_fd",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()),
    )
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ReleaseInstallError, match="backlog"):
            installer._reap_deferred_staging(parent_fd)
    finally:
        os.close(parent_fd)
    assert len(installer._deferred_staging) == release_module._MAX_DEFERRED_STAGING


@pytest.mark.parametrize(
    "window",
    [
        "sidecar-before-dir",
        "dir-before-complete-sidecar",
        "published-with-sidecar",
        "cleanup-before-sidecar",
    ],
)
def test_restart_reaper_resolves_durable_recovery_crash_windows(
    tmp_path: Path, window: str
) -> None:
    staging = tmp_path / "staging"
    releases = tmp_path / "releases"
    staging.mkdir(mode=0o700)
    releases.mkdir(mode=0o700)
    token = "a" * 16
    staging_name = f".install-{'2' * 64}-{token}"
    recovery_name = f".recovery-{token}.state"
    staging_path = staging / staging_name
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    published = releases / ("2" * 64)
    try:
        if window != "sidecar-before-dir":
            staging_path.mkdir(mode=0o700)
            metadata = staging_path.stat()
            identity = (metadata.st_dev, metadata.st_ino)
        else:
            identity = (123, 456)
        if window in {"sidecar-before-dir", "dir-before-complete-sidecar"}:
            release_module._write_recovery_intent_fd(
                parent_fd, recovery_name, staging_name
            )
        else:
            release_module._write_recovery_record_fd(
                parent_fd, recovery_name, staging_name, identity
            )
        if window == "published-with-sidecar":
            os.rename(staging_path, published)
        elif window == "cleanup-before-sidecar":
            os.rmdir(staging_path)

        restarted = ReleaseInstaller(object(), object(), releases, staging)
        restarted._reap_deferred_staging(parent_fd)
    finally:
        os.close(parent_fd)

    assert not (staging / recovery_name).exists()
    assert not staging_path.exists()
    assert published.exists() is (window == "published-with-sidecar")
    assert bool(tuple(staging.glob(".quarantine-*"))) is (
        window == "dir-before-complete-sidecar"
    )


@pytest.mark.parametrize("damage", ["corrupt", "truncated", "symlink", "permission"])
def test_restart_reaper_fails_closed_on_unsafe_recovery_sidecar(
    tmp_path: Path, damage: str
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    token = "b" * 16
    staging_name = f".install-{'2' * 64}-{token}"
    recovery_name = f".recovery-{token}.state"
    staging_path = staging / staging_name
    staging_path.mkdir(mode=0o700)
    metadata = staging_path.stat()
    identity = (metadata.st_dev, metadata.st_ino)
    recovery = staging / recovery_name
    if damage == "symlink":
        target = tmp_path / "foreign-record"
        target.write_bytes(
            release_module._recovery_record_bytes(staging_name, identity)
        )
        recovery.symlink_to(target)
    else:
        recovery.write_bytes(
            b"invalid\n"
            if damage == "corrupt"
            else (
                b"1\n"
                if damage == "truncated"
                else release_module._recovery_record_bytes(staging_name, identity)
            )
        )
        recovery.chmod(0o644 if damage == "permission" else 0o600)

    restarted = ReleaseInstaller(
        object(), object(), tmp_path / "releases", staging
    )
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ReleaseInstallError, match="recovery record"):
            restarted._reap_deferred_staging(parent_fd)
    finally:
        os.close(parent_fd)

    assert staging_path.is_dir()
    assert os.path.lexists(recovery)


def test_install_wraps_recovery_budget_expiry_as_release_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    monkeypatch.setattr(
        release_module,
        "_read_recovery_records_fd",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("budget")),
    )
    with pytest.raises(ReleaseInstallError, match="recovery"):
        ReleaseInstaller(
            Trust(), object(), tmp_path / "releases", tmp_path / "staging"
        ).install(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )


def test_install_setup_failure_without_identity_is_typed_and_restart_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    clock = [0.0]
    original_open = release_module.os.open
    original_stat = release_module.os.stat
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])

    def fail_staging_open(path, flags, *args, **kwargs):
        if (
            str(path).startswith(".install-")
            and not str(path).endswith(".lock")
            and flags & os.O_DIRECTORY
        ):
            clock[0] = 11.0
            raise OSError("injected staging open failure")
        return original_open(path, flags, *args, **kwargs)

    def fail_staging_stat(path, *args, **kwargs):
        if (
            str(path).startswith(".install-")
            and not str(path).endswith(".lock")
            and kwargs.get("dir_fd") is not None
        ):
            raise OSError("injected staging stat failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(release_module.os, "open", fail_staging_open)
    monkeypatch.setattr(release_module.os, "stat", fail_staging_stat)
    with pytest.raises(ReleaseInstallError, match="release installation failed"):
        ReleaseInstaller(Trust(), object(), releases, staging).install(
            ReleaseRequest.parse(VALID_RELEASE),
            MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 10.0),
        )
    assert tuple(staging.glob(".recovery-*.state"))
    assert tuple(staging.glob(".install-*"))


@pytest.mark.parametrize(
    "phase",
    [
        "secure-release-root",
        "secure-staging-root",
        "release-open",
        "staging-open",
        "release-fstat",
        "staging-fstat",
        "recovery-scan",
        "lock-open",
        "lock-acquire",
        "target-stat",
        "reservation-scan",
    ],
)
def test_public_install_wraps_setup_oserror_and_closes_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    original_secure_root = release_module._secure_root
    original_open = release_module.os.open
    original_fstat = release_module.os.fstat
    original_stat = release_module.os.stat
    original_close = release_module.os.close
    opened: dict[int, str] = {}
    closed: set[int] = set()

    def fail_secure_root(path, deadline=None):
        if phase == f"secure-{'release' if Path(path) == releases else 'staging'}-root":
            raise OSError(f"injected {phase} failure")
        return original_secure_root(path, deadline)

    def fail_open(path, flags, *args, **kwargs):
        label = None
        if Path(path) == releases:
            label = "release"
        elif Path(path) == staging:
            label = "staging"
        elif str(path).endswith(".lock"):
            label = "lock"
        if phase == f"{label}-open":
            raise OSError(f"injected {phase} failure")
        result = original_open(path, flags, *args, **kwargs)
        if label is not None:
            opened[result] = label
        return result

    def fail_fstat(fd):
        label = opened.get(fd)
        if phase == f"{label}-fstat":
            raise OSError(f"injected {phase} failure")
        return original_fstat(fd)

    def fail_stat(path, *args, **kwargs):
        if phase == "target-stat" and path == VALID_RELEASE["target_digest"]:
            raise OSError("injected target stat failure")
        return original_stat(path, *args, **kwargs)

    def record_close(fd):
        closed.add(fd)
        return original_close(fd)

    monkeypatch.setattr(release_module, "_secure_root", fail_secure_root)
    monkeypatch.setattr(release_module.os, "open", fail_open)
    monkeypatch.setattr(release_module.os, "fstat", fail_fstat)
    monkeypatch.setattr(release_module.os, "stat", fail_stat)
    monkeypatch.setattr(release_module.os, "close", record_close)
    if phase == "recovery-scan":
        monkeypatch.setattr(
            release_module,
            "_read_recovery_records_fd",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("injected recovery scan failure")
            ),
        )
    if phase == "reservation-scan":
        original_read_records = release_module._read_recovery_records_fd
        scans = [0]

        def fail_reservation_scan(*args, **kwargs):
            scans[0] += 1
            if scans[0] == 2:
                raise OSError("injected reservation scan failure")
            return original_read_records(*args, **kwargs)

        monkeypatch.setattr(
            release_module, "_read_recovery_records_fd", fail_reservation_scan
        )
    if phase == "lock-acquire":
        monkeypatch.setattr(
            release_module,
            "_acquire_lock",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("injected lock acquire failure")
            ),
        )

    with pytest.raises(ReleaseInstallError):
        ReleaseInstaller(Trust(), object(), releases, staging).install(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )

    assert set(opened) <= closed


def test_reservation_recovery_scan_obeys_claim_deadline_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    pulled = [False]

    class Transport:
        def pull(self, descriptor, destination, deadline):
            pulled[0] = True

    clock = [0.0]
    scans = [0]
    continued_after_expiry = [False]
    original_read_records = release_module._read_recovery_records_fd
    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])

    def expire_during_reservation(parent_fd, check, **kwargs):
        scans[0] += 1
        if scans[0] == 2:
            clock[0] = 11.0
            check()
            continued_after_expiry[0] = True
        return original_read_records(parent_fd, check, **kwargs)

    monkeypatch.setattr(
        release_module, "_read_recovery_records_fd", expire_during_reservation
    )
    with pytest.raises(ReleaseInstallError, match="deadline"):
        ReleaseInstaller(
            Trust(), Transport(), tmp_path / "releases", tmp_path / "staging"
        ).install(
            ReleaseRequest.parse(VALID_RELEASE),
            MonotonicDeadline(datetime.now(UTC) + timedelta(seconds=10), 10.0),
        )
    assert not pulled[0]
    assert not continued_after_expiry[0]


def test_live_install_never_exceeds_real_aggregate_recovery_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            observe("transport")
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    for index in range(12):
        (staging / f".unsafe-recovery-{index:016x}").write_bytes(b"held")
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    observed: dict[str, int] = {}

    def artifact_count() -> int:
        count = 0
        for entry in staging.iterdir():
            name = entry.name
            if (
                release_module._RECOVERY_NAME.fullmatch(name)
                or release_module._RECOVERY_TEMP_NAME.fullmatch(name)
                or release_module._STAGING_NAME.fullmatch(name)
                or name.startswith((".quarantine-", ".unsafe-recovery-", ".remove-"))
                or (name.startswith(".recovery-") and ".state.remove-" in name)
            ):
                count += 1
        return count

    def observe(phase: str) -> None:
        observed[phase] = artifact_count() + installer._active_staging

    original_intent = release_module._write_recovery_intent_fd
    original_mkdir = release_module.os.mkdir
    original_write = release_module._write_recovery_bytes_fd

    def observe_intent(*args, **kwargs):
        result = original_intent(*args, **kwargs)
        observe("intent")
        return result

    def observe_mkdir(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if release_module._STAGING_NAME.fullmatch(str(path)):
            observe("staging")
        return result

    def observe_write(parent_fd, record_name, data, check=None):
        result = original_write(parent_fd, record_name, data, check)
        if release_module._RECOVERY_TEMP_NAME.fullmatch(record_name):
            observe("completion-temp")
        return result

    monkeypatch.setattr(release_module, "_write_recovery_intent_fd", observe_intent)
    monkeypatch.setattr(release_module.os, "mkdir", observe_mkdir)
    monkeypatch.setattr(release_module, "_write_recovery_bytes_fd", observe_write)

    installer.install(
        ReleaseRequest.parse(VALID_RELEASE),
        datetime.now(UTC) + timedelta(seconds=5),
    )

    assert observed == {
        "intent": 14,
        "staging": 15,
        "completion-temp": 16,
        "transport": 15,
    }
    assert max(observed.values()) == release_module._MAX_DEFERRED_STAGING


def test_live_install_rejects_when_maximum_transient_footprint_would_exceed_cap(
    tmp_path: Path,
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    pulled = [False]

    class Transport:
        def pull(self, descriptor, destination, deadline):
            pulled[0] = True

    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    for index in range(13):
        (staging / f".unsafe-recovery-{index:016x}").write_bytes(b"held")
    installer = ReleaseInstaller(
        Trust(), Transport(), tmp_path / "releases", staging
    )

    with pytest.raises(ReleaseInstallError, match="backlog"):
        installer.install(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=5),
        )

    assert not pulled[0]
    assert installer._active_staging == 0
    assert not any(
        release_module._STAGING_NAME.fullmatch(entry.name)
        for entry in staging.iterdir()
    )
    assert not tuple(staging.glob(".recovery-*.state"))


def test_distinct_installers_serialize_real_install_lifecycles_by_staging_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return replace(base_descriptor, target_digest=request.target_digest)

    first_entered = threading.Event()
    release_first = threading.Event()

    class Transport:
        def pull(self, descriptor, destination, deadline):
            if descriptor.target_digest == VALID_RELEASE["target_digest"]:
                first_entered.set()
                assert release_first.wait(timeout=5)
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    first_installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    second_installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    real_flock = release_module.fcntl.flock
    second_contended = threading.Event()

    def observed_flock(descriptor, operation):
        try:
            return real_flock(descriptor, operation)
        except BlockingIOError:
            if threading.current_thread().name == "second-installer":
                second_contended.set()
            raise

    monkeypatch.setattr(release_module.fcntl, "flock", observed_flock)
    results: dict[str, ReleaseEvidence] = {}
    errors: dict[str, BaseException] = {}

    def run(label: str, installer: ReleaseInstaller, request: ReleaseRequest) -> None:
        try:
            results[label] = installer.install(
                request, datetime.now(UTC) + timedelta(seconds=5)
            )
        except BaseException as error:  # noqa: BLE001 - thread result capture
            errors[label] = error

    first_request = ReleaseRequest.parse(VALID_RELEASE)
    second_request = ReleaseRequest.parse(
        {**VALID_RELEASE, "target_digest": "4" * 64}
    )
    first = threading.Thread(
        target=run,
        args=("first", first_installer, first_request),
        name="first-installer",
    )
    second = threading.Thread(
        target=run,
        args=("second", second_installer, second_request),
        name="second-installer",
    )
    first.start()
    assert first_entered.wait(timeout=2)
    first_staging = next(staging.glob(f".install-{first_request.target_digest}-*"))
    second.start()
    try:
        contended = second_contended.wait(timeout=2)
        active_tree_survived = first_staging.is_dir()
        second_waited = second.is_alive()
    finally:
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)

    assert contended
    assert active_tree_survived
    assert second_waited
    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
    assert set(results) == {"first", "second"}


def test_inspection_cannot_reap_another_installers_active_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return replace(base_descriptor, target_digest=request.target_digest)

    entered_transport = threading.Event()
    release_transport = threading.Event()

    class Transport:
        def pull(self, descriptor, destination, deadline):
            entered_transport.set()
            assert release_transport.wait(timeout=5)
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    inspector = ReleaseInstaller(Trust(), object(), releases, staging)
    real_flock = release_module.fcntl.flock
    inspector_contended = threading.Event()

    def observed_flock(descriptor, operation):
        try:
            return real_flock(descriptor, operation)
        except BlockingIOError:
            if threading.current_thread().name == "release-inspector":
                inspector_contended.set()
            raise

    monkeypatch.setattr(release_module.fcntl, "flock", observed_flock)
    install_errors: list[BaseException] = []
    inspection_results: list[ReleaseInspection] = []

    def run_install() -> None:
        try:
            installer.install(
                ReleaseRequest.parse(VALID_RELEASE),
                datetime.now(UTC) + timedelta(seconds=5),
            )
        except BaseException as error:  # noqa: BLE001 - thread result capture
            install_errors.append(error)

    def run_inspection() -> None:
        inspection_results.append(
            inspector.inspect(
                ReleaseRequest.parse(
                    {**VALID_RELEASE, "target_digest": "4" * 64}
                ),
                datetime.now(UTC) + timedelta(seconds=5),
            )
        )

    install_thread = threading.Thread(target=run_install)
    inspect_thread = threading.Thread(
        target=run_inspection, name="release-inspector"
    )
    install_thread.start()
    assert entered_transport.wait(timeout=2)
    active_staging = next(staging.glob(f".install-{VALID_RELEASE['target_digest']}-*"))
    inspect_thread.start()
    try:
        contended = inspector_contended.wait(timeout=2)
        active_tree_survived = active_staging.is_dir()
    finally:
        release_transport.set()
        install_thread.join(timeout=5)
        inspect_thread.join(timeout=5)

    assert contended
    assert active_tree_survived
    assert not install_errors
    assert len(inspection_results) == 1


def test_inspection_observes_same_release_published_while_waiting_for_root_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    entered_transport = threading.Event()
    release_transport = threading.Event()

    class Transport:
        def pull(self, descriptor, destination, deadline):
            entered_transport.set()
            assert release_transport.wait(timeout=5)
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    inspector = ReleaseInstaller(Trust(), object(), releases, staging)
    real_flock = release_module.fcntl.flock
    inspector_contended = threading.Event()

    def observed_flock(file_descriptor, operation):
        try:
            return real_flock(file_descriptor, operation)
        except BlockingIOError:
            if threading.current_thread().name == "same-release-inspector":
                inspector_contended.set()
            raise

    monkeypatch.setattr(release_module.fcntl, "flock", observed_flock)
    install_errors: list[BaseException] = []
    inspection_results: list[ReleaseInspection] = []

    def run_install() -> None:
        try:
            installer.install(
                ReleaseRequest.parse(VALID_RELEASE),
                datetime.now(UTC) + timedelta(seconds=5),
            )
        except BaseException as error:  # noqa: BLE001 - thread result capture
            install_errors.append(error)

    def run_inspection() -> None:
        inspection_results.append(
            inspector.inspect(
                ReleaseRequest.parse(VALID_RELEASE),
                datetime.now(UTC) + timedelta(seconds=5),
            )
        )

    install_thread = threading.Thread(target=run_install)
    inspect_thread = threading.Thread(
        target=run_inspection, name="same-release-inspector"
    )
    install_thread.start()
    assert entered_transport.wait(timeout=2)
    inspect_thread.start()
    try:
        contended = inspector_contended.wait(timeout=2)
        inspection_waited = inspect_thread.is_alive()
    finally:
        release_transport.set()
        install_thread.join(timeout=5)
        inspect_thread.join(timeout=5)

    assert contended
    assert inspection_waited
    assert not install_thread.is_alive()
    assert not inspect_thread.is_alive()
    assert not install_errors
    assert (releases / VALID_RELEASE["target_digest"]).is_dir()
    assert not any(
        release_module._STAGING_NAME.fullmatch(entry.name)
        for entry in staging.iterdir()
    )
    assert not tuple(staging.glob(".recovery-*.state"))
    assert inspection_results == [
        ReleaseInspection(
            ReleaseDisposition.COMPLETED,
            ReleaseEvidence(
                "already-installed",
                "2" * 64,
                "sha256:" + "1" * 64,
                "node-runtime-v1",
            ),
        )
    ]


@pytest.mark.skipif(
    not Path("/proc/self/fd").is_dir(), reason="requires Linux fd inspection"
)
def test_inspection_closes_releases_root_fd_when_staging_root_open_fails(
    tmp_path: Path,
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    releases = tmp_path / "releases"
    staging = tmp_path / "not-a-staging-directory"
    releases.mkdir(mode=0o700)
    staging.write_bytes(b"not a directory")
    inspector = ReleaseInstaller(Trust(), object(), releases, staging)
    request = ReleaseRequest.parse(VALID_RELEASE)
    before = len(os.listdir("/proc/self/fd"))

    for _ in range(32):
        assert inspector.inspect(
            request, datetime.now(UTC) + timedelta(seconds=2)
        ) == ReleaseInspection(ReleaseDisposition.OPERATOR_INTERVENTION)

    assert len(os.listdir("/proc/self/fd")) == before


def test_inspection_holds_root_lock_through_candidate_inode_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return replace(base_descriptor, target_digest=request.target_digest)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    candidate = staging / f".install-{VALID_RELEASE['target_digest']}-{'a' * 16}"
    candidate_member = candidate / "bin/runtime-adapter"
    candidate_member.parent.mkdir(parents=True)
    candidate_member.write_bytes(b"x" * 17)
    candidate_member.chmod(0o500)
    verification_started = threading.Event()
    allow_verification = threading.Event()
    second_entered_recovery = threading.Event()
    allow_replacement = threading.Event()
    replacement_finished = threading.Event()
    original_verify = release_module._verify_release_tree_fd

    def pause_inspection_verification(root_fd, descriptor, **kwargs):
        if threading.current_thread().name == "candidate-inspector":
            verification_started.set()
            assert allow_verification.wait(timeout=5)
        return original_verify(root_fd, descriptor, **kwargs)

    monkeypatch.setattr(
        release_module, "_verify_release_tree_fd", pause_inspection_verification
    )

    class ReplacingInstaller(ReleaseInstaller):
        def _reap_deferred_staging(self, parent_fd):
            second_entered_recovery.set()
            assert allow_replacement.wait(timeout=5)
            moved = staging / ".moved-inspection-candidate"
            os.rename(candidate, moved)
            malicious = candidate / "bin/runtime-adapter"
            malicious.parent.mkdir(parents=True)
            malicious.write_bytes(b"attacker")
            malicious.chmod(0o500)
            replacement_finished.set()
            return False

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    inspection_results: list[ReleaseInspection] = []
    install_errors: list[BaseException] = []

    def inspect_candidate() -> None:
        inspection_results.append(
            ReleaseInstaller(Trust(), object(), releases, staging).inspect(
                ReleaseRequest.parse(VALID_RELEASE),
                datetime.now(UTC) + timedelta(seconds=5),
            )
        )

    def run_next_install() -> None:
        try:
            ReplacingInstaller(Trust(), Transport(), releases, staging).install(
                ReleaseRequest.parse(
                    {**VALID_RELEASE, "target_digest": "4" * 64}
                ),
                datetime.now(UTC) + timedelta(seconds=5),
            )
        except BaseException as error:  # noqa: BLE001 - thread result capture
            install_errors.append(error)

    inspector_thread = threading.Thread(
        target=inspect_candidate, name="candidate-inspector"
    )
    installer_thread = threading.Thread(target=run_next_install)
    inspector_thread.start()
    assert verification_started.wait(timeout=2)
    installer_thread.start()
    entered_before_verification = second_entered_recovery.wait(timeout=2)
    allow_replacement.set()
    if entered_before_verification:
        assert replacement_finished.wait(timeout=2)
    candidate_survived = candidate_member.read_bytes() == b"x" * 17
    allow_verification.set()
    inspector_thread.join(timeout=5)
    installer_thread.join(timeout=5)

    assert not entered_before_verification
    assert candidate_survived
    assert not inspector_thread.is_alive()
    assert not installer_thread.is_alive()
    assert not install_errors
    assert inspection_results == [
        ReleaseInspection(ReleaseDisposition.SAFE_TO_RESUME)
    ]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX advisory locks")
def test_staging_root_lifecycle_lock_is_process_safe(tmp_path: Path) -> None:
    base_descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return replace(base_descriptor, target_digest=request.target_digest)

    parent_entered = threading.Event()
    release_parent = threading.Event()

    class ParentTransport:
        def pull(self, descriptor, destination, deadline):
            parent_entered.set()
            assert release_parent.wait(timeout=8)
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    control_read, control_write = os.pipe()
    status_read, status_write = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(control_write)
        os.close(status_read)
        real_flock = release_module.fcntl.flock
        reported_contention = False

        def child_flock(descriptor, operation):
            nonlocal reported_contention
            try:
                return real_flock(descriptor, operation)
            except BlockingIOError:
                if not reported_contention:
                    os.write(status_write, b"B")
                    reported_contention = True
                raise

        class ChildTransport:
            def pull(self, descriptor, destination, deadline):
                os.write(status_write, b"T")
                member = destination / "bin/runtime-adapter"
                member.parent.mkdir()
                member.write_bytes(b"x" * 17)
                member.chmod(0o500)

        try:
            release_module.fcntl.flock = child_flock
            os.write(status_write, b"R")
            if os.read(control_read, 1) != b"G":
                os._exit(2)
            request = ReleaseRequest.parse(
                {**VALID_RELEASE, "target_digest": "4" * 64}
            )
            ReleaseInstaller(Trust(), ChildTransport(), releases, staging).install(
                request, datetime.now(UTC) + timedelta(seconds=6)
            )
            os.write(status_write, b"S")
            os._exit(0)
        except BaseException:  # noqa: BLE001 - report isolated child failure
            os.write(status_write, b"E")
            os._exit(1)

    os.close(control_read)
    os.close(status_write)
    assert os.read(status_read, 1) == b"R"
    errors: list[BaseException] = []

    def run_parent() -> None:
        try:
            ReleaseInstaller(Trust(), ParentTransport(), releases, staging).install(
                ReleaseRequest.parse(VALID_RELEASE),
                datetime.now(UTC) + timedelta(seconds=8),
            )
        except BaseException as error:  # noqa: BLE001 - thread result capture
            errors.append(error)

    parent = threading.Thread(target=run_parent)
    parent.start()
    assert parent_entered.wait(timeout=2)
    parent_staging = next(staging.glob(f".install-{VALID_RELEASE['target_digest']}-*"))
    os.write(control_write, b"G")
    ready, _, _ = select.select([status_read], [], [], 2)
    first_child_status = os.read(status_read, 1) if ready else b""
    active_tree_survived = parent_staging.is_dir()
    release_parent.set()
    parent.join(timeout=8)
    _, child_status = os.waitpid(child_pid, 0)
    remaining_status = os.read(status_read, 8)
    os.close(control_write)
    os.close(status_read)

    assert first_child_status == b"B"
    assert active_tree_survived
    assert not parent.is_alive()
    assert not errors
    assert os.waitstatus_to_exitcode(child_status) == 0
    assert remaining_status == b"TS"


@pytest.mark.parametrize("damage", ["symlink", "mode"])
def test_staging_root_lifecycle_lock_is_fixed_nofollow_and_mode_0600(
    tmp_path: Path, damage: str
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    lock_path = staging / ".install-recovery.lock"
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"unchanged")
    if damage == "symlink":
        lock_path.symlink_to(foreign)
    else:
        lock_path.write_bytes(b"")
        lock_path.chmod(0o644)

    with pytest.raises(ReleaseInstallError, match="setup"):
        ReleaseInstaller(Trust(), object(), releases, staging).install(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )

    assert foreign.read_bytes() == b"unchanged"


def test_staging_token_oserror_is_typed_and_releases_reservation_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    installer = ReleaseInstaller(
        Trust(), Transport(), tmp_path / "releases", tmp_path / "staging"
    )
    real_token_hex = release_module.secrets.token_hex
    calls = [0]

    def fail_once(size: int) -> str:
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("injected token failure")
        return real_token_hex(size)

    monkeypatch.setattr(release_module.secrets, "token_hex", fail_once)
    request = ReleaseRequest.parse(VALID_RELEASE)
    with pytest.raises(ReleaseInstallError, match="installation failed"):
        installer.install(request, datetime.now(UTC) + timedelta(seconds=5))

    assert installer._active_staging == 0
    assert not any(
        release_module._STAGING_NAME.fullmatch(entry.name)
        for entry in (tmp_path / "staging").iterdir()
    )
    assert not tuple((tmp_path / "staging").glob(".recovery-*"))
    assert installer.install(
        request, datetime.now(UTC) + timedelta(seconds=5)
    ).status == "installed"


def test_staging_token_deadline_crossing_releases_reservation_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    clock = [0.0]
    real_token_hex = release_module.secrets.token_hex
    calls = [0]

    def expire_once(size: int) -> str:
        calls[0] += 1
        token = real_token_hex(size)
        if calls[0] == 1:
            clock[0] = 11.0
        return token

    monkeypatch.setattr("vonk_agent.deadlines.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(release_module.secrets, "token_hex", expire_once)
    installer = ReleaseInstaller(
        Trust(), Transport(), tmp_path / "releases", tmp_path / "staging"
    )
    request = ReleaseRequest.parse(VALID_RELEASE)
    deadline = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=10), 10.0
    )
    with pytest.raises(ReleaseInstallError, match="deadline"):
        installer.install(request, deadline)

    assert installer._active_staging == 0
    assert not any(
        release_module._STAGING_NAME.fullmatch(entry.name)
        for entry in (tmp_path / "staging").iterdir()
    )
    assert not tuple((tmp_path / "staging").glob(".recovery-*"))
    clock[0] = 0.0
    assert installer.install(request, deadline).status == "installed"


def test_quarantine_token_generation_cannot_cross_recovery_budget_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    staging_name = f".install-{'2' * 64}-{'a' * 16}"
    (staging / staging_name).mkdir(mode=0o700)
    expired = [False]
    renamed = [False]

    def check() -> None:
        if expired[0]:
            raise TimeoutError("recovery budget elapsed")

    def expire_budget(_size: int) -> str:
        expired[0] = True
        return "b" * 16

    def record_rename(*args, **kwargs):
        renamed[0] = True

    monkeypatch.setattr(release_module.secrets, "token_hex", expire_budget)
    monkeypatch.setattr(release_module, "_rename_noreplace", record_rename)
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(TimeoutError, match="budget"):
            release_module._quarantine_unproven_staging_fd(
                parent_fd, staging_name, check
            )
    finally:
        os.close(parent_fd)
    assert not renamed[0]


def test_recovery_completion_never_overwrites_replaced_intent(
    tmp_path: Path
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    token = "d" * 16
    staging_name = f".install-{'2' * 64}-{token}"
    record_name = f".recovery-{token}.state"
    staging_path = staging / staging_name
    staging_path.mkdir(mode=0o700)
    staging_metadata = staging_path.stat()
    staging_identity = (staging_metadata.st_dev, staging_metadata.st_ino)
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    moved = staging / ".moved-original-intent"
    replacement = staging / record_name
    try:
        intent_identity = release_module._write_recovery_intent_fd(
            parent_fd, record_name, staging_name
        )
        os.rename(replacement, moved)
        replacement.write_bytes(b"foreign replacement\n")
        replacement.chmod(0o600)
        replacement_identity = replacement.stat().st_ino
        with pytest.raises(ReleaseInstallError, match="identity"):
            release_module._complete_recovery_record_fd(
                parent_fd,
                record_name,
                staging_name,
                staging_identity,
                intent_identity=intent_identity,
                check=lambda: None,
            )
    finally:
        os.close(parent_fd)
    assert replacement.read_bytes() == b"foreign replacement\n"
    assert replacement.stat().st_ino == replacement_identity
    assert moved.is_file()


def test_recovery_completion_requires_captured_intent_to_still_exist(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    token = "f" * 16
    staging_name = f".install-{'2' * 64}-{token}"
    record_name = f".recovery-{token}.state"
    staging_path = staging / staging_name
    staging_path.mkdir(mode=0o700)
    metadata = staging_path.stat()
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        intent_identity = release_module._write_recovery_intent_fd(
            parent_fd, record_name, staging_name
        )
        os.unlink(record_name, dir_fd=parent_fd)
        with pytest.raises(ReleaseInstallError, match="identity"):
            release_module._complete_recovery_record_fd(
                parent_fd,
                record_name,
                staging_name,
                (metadata.st_dev, metadata.st_ino),
                intent_identity=intent_identity,
                check=lambda: None,
            )
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize("kind", ["regular", "symlink", "hardlink"])
def test_recursive_leaf_cleanup_preserves_name_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path / "staging"
    tree = root / "owned"
    tree.mkdir(parents=True)
    child = tree / "leaf"
    outside = tmp_path / "outside"
    outside.write_bytes(b"owned")
    if kind == "regular":
        child.write_bytes(b"owned")
    elif kind == "symlink":
        child.symlink_to(outside)
    else:
        os.link(outside, child)
    tree_metadata = tree.stat()
    tree_identity = (tree_metadata.st_dev, tree_metadata.st_ino)
    moved = tmp_path / f"moved-{kind}"
    substituted = [False]
    original_unlink = release_module.os.unlink
    original_rename = release_module._rename_noreplace

    def substitute() -> None:
        if substituted[0]:
            return
        substituted[0] = True
        os.rename(child, moved)
        if kind == "symlink":
            child.symlink_to(tmp_path / "foreign-target")
        else:
            child.write_bytes(b"foreign")

    def unlink_after_substitution(path, *args, **kwargs):
        if path == "leaf":
            substitute()
        return original_unlink(path, *args, **kwargs)

    def rename_after_substitution(source_fd, source, destination_fd, destination):
        if source == "leaf":
            substitute()
        return original_rename(source_fd, source, destination_fd, destination)

    monkeypatch.setattr(release_module.os, "unlink", unlink_after_substitution)
    monkeypatch.setattr(release_module, "_rename_noreplace", rename_after_substitution)
    parent_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            release_module._remove_bound_tree_fd(
                parent_fd, tree.name, tree_identity, lambda: None
            )
        except (OSError, ReleaseInstallError):
            pass
    finally:
        os.close(parent_fd)

    assert moved.exists() or moved.is_symlink()
    assert child.exists() or child.is_symlink()
    if kind != "symlink":
        assert child.read_bytes() == b"foreign"


def test_restart_reaper_consumes_complete_temp_without_quarantine_accumulation(
    tmp_path: Path
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    staging = tmp_path / "staging"
    releases = tmp_path / "releases"
    staging.mkdir(mode=0o700)
    releases.mkdir(mode=0o700)
    request = ReleaseRequest.parse(VALID_RELEASE)
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index in range(3):
            token = f"{index + 1:016x}"
            staging_name = f".install-{'2' * 64}-{token}"
            state_name = f".recovery-{token}.state"
            temp_name = f".recovery-{token}.new"
            staging_path = staging / staging_name
            staging_path.mkdir(mode=0o700)
            metadata = staging_path.stat()
            identity = (metadata.st_dev, metadata.st_ino)
            release_module._write_recovery_intent_fd(
                parent_fd, state_name, staging_name
            )
            release_module._write_recovery_bytes_fd(
                parent_fd,
                temp_name,
                release_module._recovery_record_bytes(staging_name, identity),
            )
            inspection = ReleaseInstaller(
                Trust(), object(), releases, staging
            ).inspect(request, datetime.now(UTC) + timedelta(seconds=2))
            assert inspection.disposition is ReleaseDisposition.SAFE_TO_RESUME
            assert {entry.name for entry in staging.iterdir()} == {
                release_module._INSTALL_RECOVERY_LOCK_NAME
            }
    finally:
        os.close(parent_fd)


@pytest.mark.parametrize("damage", ["corrupt", "symlink"])
def test_restart_reaper_fails_closed_without_deleting_unsafe_temp(
    tmp_path: Path, damage: str
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    staging = tmp_path / "staging"
    releases = tmp_path / "releases"
    staging.mkdir(mode=0o700)
    releases.mkdir(mode=0o700)
    token = "e" * 16
    staging_name = f".install-{'2' * 64}-{token}"
    state_name = f".recovery-{token}.state"
    temp_name = f".recovery-{token}.new"
    staging_path = staging / staging_name
    staging_path.mkdir(mode=0o700)
    metadata = staging_path.stat()
    identity = (metadata.st_dev, metadata.st_ino)
    parent_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
    try:
        release_module._write_recovery_intent_fd(
            parent_fd, state_name, staging_name
        )
        temp = staging / temp_name
        if damage == "corrupt":
            temp.write_bytes(b"not canonical\n")
            temp.chmod(0o600)
        else:
            foreign = tmp_path / "foreign-temp"
            foreign.write_bytes(
                release_module._recovery_record_bytes(staging_name, identity)
            )
            temp.symlink_to(foreign)
        inspection = ReleaseInstaller(
            Trust(), object(), releases, staging
        ).inspect(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )
    finally:
        os.close(parent_fd)

    assert inspection.disposition is ReleaseDisposition.OPERATOR_INTERVENTION
    assert os.path.lexists(staging / temp_name)
    assert staging_path.is_dir()


@pytest.mark.parametrize("branch", ["initial-existing", "rename-race", "inspect"])
def test_every_idempotent_branch_rejects_destination_identity_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    destination = releases / ("2" * 64)
    replacement = tmp_path / "replacement"
    moved = tmp_path / "moved"

    def make_installed(root: Path) -> None:
        member = root / "bin/runtime-adapter"
        member.parent.mkdir(parents=True, mode=0o700)
        member.write_bytes(b"x" * 17)
        member.chmod(0o500)
        release_module._write_receipt(root, descriptor)

    if branch != "rename-race":
        make_installed(destination)
        make_installed(replacement)

    original_loads = release_module.json.loads
    swapped = False

    def loads_then_swap(raw, **kwargs):
        nonlocal swapped
        document = original_loads(raw, **kwargs)
        if not swapped and destination.exists() and replacement.exists():
            swapped = True
            os.rename(destination, moved)
            os.rename(replacement, destination)
        return document

    monkeypatch.setattr(release_module.json, "loads", loads_then_swap)
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    request = ReleaseRequest.parse(VALID_RELEASE)
    deadline = datetime.now(UTC) + timedelta(seconds=2)

    if branch == "rename-race":
        original_rename = release_module._rename_noreplace

        def competing_publish(*args):
            if args[3] != "2" * 64:
                return original_rename(*args)
            make_installed(destination)
            make_installed(replacement)
            raise FileExistsError(request.target_digest)

        monkeypatch.setattr(
            release_module, "_rename_noreplace", competing_publish
        )
        with pytest.raises(ReleaseInstallError, match="identity"):
            installer.install(request, deadline)
    elif branch == "initial-existing":
        with pytest.raises(ReleaseInstallError, match="identity"):
            installer.install(request, deadline)
    else:
        assert (
            installer.inspect(request, deadline).disposition
            is ReleaseDisposition.OPERATOR_INTERVENTION
        )

    assert swapped
    assert destination.stat().st_ino != moved.stat().st_ino


@pytest.mark.parametrize(
    "attack",
    ["unexpected", "symlink", "hardlink", "fifo", "mode", "case-collision"],
)
def test_release_tree_rejects_untrusted_member_attacks(tmp_path: Path, attack: str) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())
    root = tmp_path / "tree"
    member = root / "bin/runtime-adapter"
    member.parent.mkdir(parents=True)
    member.write_bytes(b"x" * 17)
    member.chmod(0o500)
    if attack == "unexpected":
        (root / "extra").write_text("x")
    elif attack == "symlink":
        member.unlink()
        member.symlink_to("/etc/passwd")
    elif attack == "hardlink":
        os.link(member, tmp_path / "outside-link")
    elif attack == "fifo":
        member.unlink()
        os.mkfifo(member)
    elif attack == "mode":
        member.chmod(0o700)
    else:
        collision = root / "BIN/runtime-adapter"
        collision.parent.mkdir()
        collision.write_bytes(b"x" * 17)

    with pytest.raises(ReleaseInstallError):
        verify_release_tree(root, descriptor)


@pytest.mark.parametrize(
    "path",
    [
        "/bin/runtime-adapter",
        "../runtime-adapter",
        "bin\\runtime-adapter",
        "bin/e\u0301-adapter",
    ],
)
def test_descriptor_rejects_absolute_parent_backslash_and_non_nfc_paths(path: str) -> None:
    document = _descriptor()
    document["members"][0]["path"] = path
    with pytest.raises(ReleaseValidationError):
        ReleaseDescriptor.parse(document)


@pytest.mark.parametrize("second_path", ["bin/runtime-adapter", "BIN/runtime-adapter"])
def test_descriptor_rejects_duplicate_and_casefolded_member_identity(second_path: str) -> None:
    document = _descriptor()
    document["target_length"] = 34
    document["members"].append(document["members"][0] | {"path": second_path})
    document["members"].sort(key=lambda item: item["path"])
    with pytest.raises(ReleaseValidationError):
        ReleaseDescriptor.parse(document)


@pytest.mark.parametrize("limit", ["count", "file", "aggregate"])
def test_descriptor_rejects_member_count_file_and_aggregate_limits(limit: str) -> None:
    document = _descriptor()
    if limit == "count":
        document["members"] = [
            document["members"][0]
            | {"path": f"bin/member-{index:03d}", "size": 0}
            for index in range(257)
        ]
        document["members"][0]["size"] = 1
        document["target_length"] = 1
    elif limit == "file":
        document["members"][0]["size"] = 256 * 1024 * 1024 + 1
        document["target_length"] = 1
    else:
        document["members"] = [
            document["members"][0]
            | {"path": f"bin/member-{index}", "size": 256 * 1024 * 1024}
            for index in range(5)
        ]
        document["target_length"] = 1
    with pytest.raises(ReleaseValidationError):
        ReleaseDescriptor.parse(document)


def test_descriptor_rejects_a_receipt_that_cannot_be_reverified() -> None:
    document = _descriptor()
    document["members"] = [
        document["members"][0]
        | {"path": f"bin/{index:03d}-" + "a" * 300, "size": 0}
        for index in range(256)
    ]
    document["members"][0]["size"] = 1
    document["target_length"] = 1
    with pytest.raises(ReleaseValidationError, match="receipt"):
        ReleaseDescriptor.parse(document)


@pytest.mark.parametrize("owner_field", ["uid", "gid"])
def test_release_tree_rejects_wrong_signed_owner(tmp_path: Path, owner_field: str) -> None:
    document = _descriptor()
    document["members"][0][owner_field] = (
        document["members"][0][owner_field] + 1
    ) % 65536
    descriptor = ReleaseDescriptor.parse(document)
    member = tmp_path / "tree/bin/runtime-adapter"
    member.parent.mkdir(parents=True)
    member.write_bytes(b"x" * 17)
    member.chmod(0o500)
    with pytest.raises(ReleaseInstallError):
        verify_release_tree(tmp_path / "tree", descriptor)


def test_release_tree_rejects_unix_socket_and_device_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())
    root = tmp_path / "tree"
    member = root / "bin/runtime-adapter"
    member.parent.mkdir(parents=True)
    unix_socket = socket.socket(socket.AF_UNIX)
    try:
        unix_socket.bind(str(member))
        with pytest.raises(ReleaseInstallError):
            verify_release_tree(root, descriptor)
    finally:
        unix_socket.close()
    member.unlink()
    member.write_bytes(b"x" * 17)
    member.chmod(0o500)
    original_open = release_module.os.open

    def device_open(path, flags, *args, **kwargs):
        if path == "runtime-adapter" and kwargs.get("dir_fd") is not None:
            return original_open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(release_module.os, "open", device_open)
    with pytest.raises(ReleaseInstallError):
        verify_release_tree(root, descriptor)


def test_release_tree_rejects_unsafe_directory_mode_and_sparse_member(tmp_path: Path) -> None:
    descriptor_document = _descriptor()
    descriptor_document["target_length"] = 1024 * 1024
    descriptor_document["members"][0]["size"] = 1024 * 1024
    descriptor_document["members"][0]["sha256"] = hashlib.sha256(
        b"\0" * (1024 * 1024)
    ).hexdigest()
    descriptor = ReleaseDescriptor.parse(descriptor_document)
    root = tmp_path / "tree"
    member = root / "bin/runtime-adapter"
    member.parent.mkdir(parents=True)
    with member.open("wb") as stream:
        stream.truncate(1024 * 1024)
    member.chmod(0o500)

    with pytest.raises(ReleaseInstallError):
        verify_release_tree(root, descriptor)

    member.chmod(0o600)
    member.write_bytes(b"\0" * (1024 * 1024))
    member.chmod(0o500)
    member.parent.chmod(0o777)
    with pytest.raises(ReleaseInstallError):
        verify_release_tree(root, descriptor)


def test_installer_never_replaces_a_dangling_preexisting_destination(tmp_path: Path) -> None:
    class Trust:
        def authorize(self, request, deadline):
            return ReleaseDescriptor.parse(_descriptor())

    class Transport:
        called = False

        def pull(self, descriptor, destination, deadline):
            self.called = True

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    destination = releases / ("2" * 64)
    destination.symlink_to(tmp_path / "missing")
    transport = Transport()

    with pytest.raises(ReleaseInstallError):
        ReleaseInstaller(Trust(), transport, releases, staging).install(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )

    assert destination.is_symlink()
    assert not transport.called


def test_installer_serializes_same_release_transport(tmp_path: Path) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def __init__(self):
            self.active = 0
            self.maximum = 0
            self.calls = 0
            self.guard = threading.Lock()

        def pull(self, descriptor, destination, deadline):
            with self.guard:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                self.calls += 1
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)
            time.sleep(0.05)
            with self.guard:
                self.active -= 1

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    transport = Transport()
    installer = ReleaseInstaller(Trust(), transport, releases, staging)
    request = ReleaseRequest.parse(VALID_RELEASE)
    errors: list[Exception] = []

    def install() -> None:
        try:
            installer.install(request, datetime.now(UTC) + timedelta(seconds=2))
        except Exception as error:  # noqa: BLE001 - report thread failures
            errors.append(error)

    threads = [threading.Thread(target=install) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert transport.maximum == 1
    assert transport.calls == 1


def test_installer_lock_wait_is_bounded_by_claim_deadline(tmp_path: Path) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        called = False

        def pull(self, descriptor, destination, deadline):
            self.called = True

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    lock = os.open(
        releases / (".install-" + "2" * 64 + ".lock"),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    fcntl.flock(lock, fcntl.LOCK_EX)
    started = time.monotonic()
    try:
        with pytest.raises(ReleaseInstallError, match="deadline"):
            ReleaseInstaller(Trust(), Transport(), releases, staging).install(
                ReleaseRequest.parse(VALID_RELEASE),
                datetime.now(UTC) + timedelta(milliseconds=50),
            )
    finally:
        os.close(lock)
    assert time.monotonic() - started < 0.5


def test_monotonic_deadline_cannot_be_extended_by_backward_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = MonotonicDeadline.bind(datetime.now(UTC) + timedelta(seconds=1))

    class BackwardClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2000, 1, 1, tzinfo=UTC)

    monkeypatch.setattr("vonk_agent.deadlines.datetime", BackwardClock)
    monkeypatch.setattr(
        "vonk_agent.deadlines.time.monotonic",
        lambda: fixed.absolute_monotonic + 0.001,
    )
    with pytest.raises(DeadlineBindingError):
        fixed.check()


def test_slow_trust_stage_cannot_start_transport_after_total_deadline(tmp_path: Path) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            time.sleep(0.03)
            return descriptor

    class Transport:
        called = False

        def pull(self, descriptor, destination, deadline):
            self.called = True

    transport = Transport()
    fixed = MonotonicDeadline(
        datetime.now(UTC) + timedelta(seconds=1), time.monotonic() + 0.01
    )
    with pytest.raises(ReleaseInstallError, match="deadline"):
        ReleaseInstaller(
            Trust(), transport, tmp_path / "releases", tmp_path / "staging"
        ).install(ReleaseRequest.parse(VALID_RELEASE), fixed)
    assert not transport.called


def test_release_inspection_considers_only_matching_digest_staging(tmp_path: Path) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            raise AssertionError("inspection must not pull")

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    releases.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    other = staging / (".install-" + "9" * 64 + "-" + "b" * 16)
    member = other / "bin/runtime-adapter"
    member.parent.mkdir(parents=True)
    member.write_bytes(b"x" * 17)
    member.chmod(0o500)
    installer = ReleaseInstaller(Trust(), Transport(), releases, staging)
    request = ReleaseRequest.parse(VALID_RELEASE)

    assert installer.inspect(
        request, datetime.now(UTC) + timedelta(seconds=2)
    ).disposition is ReleaseDisposition.OPERATOR_INTERVENTION

    matching = staging / (".install-" + "2" * 64 + "-" + "c" * 16)
    matching_member = matching / "bin/runtime-adapter"
    matching_member.parent.mkdir(parents=True)
    matching_member.write_bytes(b"x" * 17)
    matching_member.chmod(0o500)
    assert installer.inspect(
        request, datetime.now(UTC) + timedelta(seconds=2)
    ).disposition is ReleaseDisposition.SAFE_TO_RESUME


def test_publication_detects_but_never_deletes_foreign_staging_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = ReleaseDescriptor.parse(_descriptor())

    class Trust:
        def authorize(self, request, deadline):
            return descriptor

    class Transport:
        def pull(self, descriptor, destination, deadline):
            member = destination / "bin/runtime-adapter"
            member.parent.mkdir()
            member.write_bytes(b"x" * 17)
            member.chmod(0o500)

    releases = tmp_path / "releases"
    staging = tmp_path / "staging"
    original_rename = release_module._rename_noreplace

    def substitute_then_rename(
        source_parent_fd, source_name, destination_parent_fd, destination_name
    ):
        if destination_name != "2" * 64:
            return original_rename(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
        source = Path(f"/proc/self/fd/{source_parent_fd}/{source_name}")
        backup = source.with_name(".attacker-moved-verified-tree")
        os.rename(source, backup)
        source.mkdir(mode=0o700)
        malicious = source / "bin/runtime-adapter"
        malicious.parent.mkdir()
        malicious.write_bytes(b"attacker")
        malicious.chmod(0o500)
        original_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
        )

    monkeypatch.setattr(
        release_module, "_rename_noreplace", substitute_then_rename
    )
    with pytest.raises(ReleaseInstallError, match="identity"):
        ReleaseInstaller(Trust(), Transport(), releases, staging).install(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )

    foreign = releases / ("2" * 64)
    assert (foreign / "bin/runtime-adapter").read_bytes() == b"attacker"
    with pytest.raises(ReleaseInstallError):
        ReleaseInstaller(Trust(), Transport(), releases, staging).install(
            ReleaseRequest.parse(VALID_RELEASE),
            datetime.now(UTC) + timedelta(seconds=2),
        )
