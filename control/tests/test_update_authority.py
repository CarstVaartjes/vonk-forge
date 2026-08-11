from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from securesystemslib.signer import CryptoSigner
from tuf.api.metadata import (
    Metadata,
    MetaFile,
    Root,
    Snapshot,
    TargetFile,
    Targets,
    Timestamp,
)
from vonk_agent_protocol import canonical_message
from vonk_control.update_authority import (
    PublishedTUFReleaseSource,
    UpdateAuthorizationAuthority,
    UpdateAuthorizationError,
    _PublishedTufFetcher,
    snapshot_public_trust_root,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
OPERATION_ID = "6eeefc54-2e98-4aee-872a-c9a81463cc23"
FENCE = "69d77209-94b8-47b2-88ef-b49c74df9e55"


def _platform_manifest() -> bytes:
    def artifact(name: str, digest: str) -> dict[str, object]:
        return {
            "name": name,
            "provenance_sha256": "d" * 64,
            "reference": f"registry.example/vonk-forge/releases@sha256:{digest}",
            "sbom_sha256": "e" * 64,
            "sha256": digest,
            "size": 1024,
        }

    document = {
        "agents": [
            {
                "architecture": "linux-arm64",
                "artifact": artifact("agent-linux-arm64", "a" * 64),
                "payload": {"name": "vonk-agent", "sha256": "b" * 64, "size": 4096},
                "protocol": {"maximum": 2, "minimum": 1},
            }
        ],
        "build_digest": "sha256:" + "c" * 64,
        "deployment_bundle": {
            "layer_digest": "sha256:" + "a" * 64,
            "layer_media_type": "application/vnd.vonk-forge.control-deployment.v1.tar",
            "layer_size": 1048576,
            "manifest_digest": "sha256:" + "f" * 64,
            "manifest_media_type": "application/vnd.oci.image.manifest.v1+json",
            "manifest_size": 4096,
            "reference": "registry.example/vonk-forge/control@sha256:" + "f" * 64,
        },
        "host_updater_abi": {"maximum": 2, "minimum": 1},
        "control": {
            "assets": [artifact("web", "f" * 64)],
            "config_version": 1,
            "images": {
                "api": artifact("api", "f" * 64),
                "worker": artifact("worker", "f" * 64),
            },
            "protocol": {"maximum": 1, "minimum": 1},
        },
        "database": {
            "contract_revision": None,
            "expand_revision": "0011_update_rollouts",
            "predecessor_compatible": True,
        },
        "platform_version": "1.2.3",
        "rollback": {
            "predecessors": [
                {
                    "build_digest": "sha256:" + "0" * 64,
                    "deployment_bundle_digest": "sha256:" + "1" * 64,
                    "release_digest": "sha256:" + "2" * 64,
                    "target_name": "platform/releases/1.2.2/" + "3" * 64 + ".json",
                    "target_sha256": "3" * 64,
                }
            ]
        },
        "schema_version": 2,
        "supervisors": [
            {
                "architecture": "linux-arm64",
                "artifact": artifact("supervisor-linux-arm64", "f" * 64),
                "payload": {"name": "supervisor", "sha256": "f" * 64, "size": 4096},
            }
        ],
        "tooling": [
            {
                "architecture": "linux-arm64",
                "artifact": artifact("tooling-linux-arm64", "f" * 64),
                "payload": {"name": "tooling", "sha256": "f" * 64, "size": 4096},
            }
        ],
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


class ReleaseSource:
    def __init__(self) -> None:
        self.target = _platform_manifest()
        self.target_names: list[str] = []

    def refresh(self, target_name: str) -> tuple[bytes, int]:
        self.target_names.append(target_name)
        return self.target, 7


class InvalidTargetsVersionReleaseSource(ReleaseSource):
    def __init__(self, targets_version: object) -> None:
        super().__init__()
        self.targets_version = targets_version

    def refresh(self, target_name: str) -> tuple[bytes, int]:
        self.target_names.append(target_name)
        return self.target, self.targets_version  # type: ignore[return-value]


def _target_name(raw: bytes | None = None, *, version: str = "1.2.3") -> str:
    target = _platform_manifest() if raw is None else raw
    return f"platform/releases/{version}/{hashlib.sha256(target).hexdigest()}.json"


def _authority(
    tmp_path: Path,
) -> tuple[UpdateAuthorizationAuthority, ed25519.Ed25519PrivateKey]:
    key = ed25519.Ed25519PrivateKey.generate()
    key_path = tmp_path / "update-authority.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o400)
    return UpdateAuthorizationAuthority.from_private_key_file(
        key_path,
        release_source=ReleaseSource(),
    ), key


def _payload() -> dict[str, object]:
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


def test_authority_exports_only_the_canonical_pinned_public_key(tmp_path: Path) -> None:
    authority, key = _authority(tmp_path)
    authority.refresh_and_validate(_payload(), target_name=_target_name())
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )

    document = authority.public_authority_document()

    assert document == {
        "algorithm": "ed25519",
        "key_id": hashlib.sha256(public).hexdigest(),
        "public_key": public.hex(),
        "schema_version": 1,
    }
    assert authority.public_authority_bytes() == canonical_message(document) + b"\n"
    assert b"PRIVATE" not in authority.public_authority_bytes()


def test_authority_signs_exact_fenced_tuf_and_slot_bindings(tmp_path: Path) -> None:
    authority, key = _authority(tmp_path)
    prepared = authority.refresh_and_validate(_payload(), target_name=_target_name())

    payload = authority.authorize(
        _payload(),
        operation_id=OPERATION_ID,
        fence=FENCE,
        expires_at=int(NOW.timestamp()) + 600,
        previous_slot="A",
        previous_sha256="d" * 64,
        previous_generation=4,
        node_id="spk_" + "a" * 32,
        attempt=1,
        claim_deadline=int(NOW.timestamp()) + 600,
        prepared=prepared,
        now=NOW,
    )

    assert set(payload) == {"artifact", "receipt", "release", "signature"}
    receipt = payload["receipt"]
    assert receipt == {
        "architecture": "linux-arm64",
        "attempt": 1,
        "build_digest": "sha256:" + "c" * 64,
        "claim_deadline": int(NOW.timestamp()) + 600,
        "expires_at": int(NOW.timestamp()) + 600,
        "fence": FENCE,
        "node_id": "spk_" + "a" * 32,
        "oci_manifest_digest": "sha256:" + "a" * 64,
        "operation_id": OPERATION_ID,
        "payload_name": "vonk-agent",
        "platform_target_name": _target_name(),
        "platform_target_sha256": hashlib.sha256(_platform_manifest()).hexdigest(),
        "platform_version": "1.2.3",
        "previous_sha256": "d" * 64,
        "previous_generation": 4,
        "previous_slot": "A",
        "sha256": "b" * 64,
        "size": 4096,
        "target_slot": "B",
        "tuf_targets_version": 7,
    }
    signature = payload["signature"]
    key.public_key().verify(
        bytes.fromhex(signature["value"]),
        (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    assert signature["algorithm"] == "ed25519"
    assert signature["key_id"] == authority.key_id


@pytest.mark.parametrize("fault", ("target", "metadata", "expiry", "payload"))
def test_authority_fails_closed_before_signing_untrusted_input(
    tmp_path: Path, fault: str
) -> None:
    authority, _ = _authority(tmp_path)
    prepared = authority.refresh_and_validate(_payload(), target_name=_target_name())
    payload = _payload()
    expires_at = int(NOW.timestamp()) + 600
    if fault == "target":
        payload["artifact"]["payload_sha256"] = "0" * 64
    elif fault == "metadata":
        prepared = object()
    elif fault == "expiry":
        expires_at += 1
    else:
        payload["unexpected"] = True

    with pytest.raises(UpdateAuthorizationError):
        authority.authorize(
            payload,
            operation_id=str(uuid.uuid4()),
            fence=str(uuid.uuid4()),
            expires_at=expires_at,
            previous_slot="A",
            previous_sha256="d" * 64,
            previous_generation=4,
            node_id="spk_" + "a" * 32,
            attempt=1,
            claim_deadline=expires_at,
            prepared=prepared,
            now=NOW,
        )


@pytest.mark.parametrize("targets_version", (True, 0, 2_147_483_648))
def test_authority_rejects_invalid_tuf_targets_version_before_preparation(
    tmp_path: Path, targets_version: object
) -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    key_path = tmp_path / "update-authority.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o400)
    authority = UpdateAuthorizationAuthority.from_private_key_file(
        key_path,
        release_source=InvalidTargetsVersionReleaseSource(targets_version),
    )

    with pytest.raises(UpdateAuthorizationError, match="targets version"):
        authority.refresh_and_validate(_payload(), target_name=_target_name())


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("target_name", "platform-release.json"),
        ("target_sha256", "0" * 64),
        ("targets_version", True),
    ),
)
def test_authority_revalidates_prepared_tuf_identity_before_signing(
    tmp_path: Path, field: str, value: object
) -> None:
    authority, _ = _authority(tmp_path)
    prepared = authority.refresh_and_validate(_payload(), target_name=_target_name())
    prepared = replace(prepared, **{field: value})
    expires_at = int(NOW.timestamp()) + 60

    with pytest.raises(UpdateAuthorizationError, match="prepared"):
        authority.authorize(
            _payload(),
            operation_id=OPERATION_ID,
            fence=FENCE,
            expires_at=expires_at,
            previous_slot="A",
            previous_sha256="d" * 64,
            previous_generation=4,
            node_id="spk_" + "a" * 32,
            attempt=1,
            claim_deadline=expires_at,
            prepared=prepared,
            now=NOW,
        )


def test_authority_signs_operator_rollback_bound_to_current_node_state(
    tmp_path: Path,
) -> None:
    authority, key = _authority(tmp_path)
    expires_at = int(NOW.timestamp()) + 60

    payload = authority.authorize_rollback(
        operation_id=OPERATION_ID,
        fence=FENCE,
        expires_at=expires_at,
        current_slot="B",
        current_sha256="a" * 64,
        current_generation=7,
        node_id="spk_" + "a" * 32,
        attempt=1,
        claim_deadline=expires_at,
        now=NOW,
    )

    receipt = payload["receipt"]
    assert receipt["action"] == "operator-rollback"
    assert receipt["current_generation"] == 7
    key.public_key().verify(
        bytes.fromhex(payload["signature"]["value"]),
        (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )


def test_authority_rejects_boolean_rollback_attempt(tmp_path: Path) -> None:
    authority, _ = _authority(tmp_path)
    expires_at = int(NOW.timestamp()) + 60

    with pytest.raises(UpdateAuthorizationError, match="claim binding"):
        authority.authorize_rollback(
            operation_id=OPERATION_ID,
            fence=FENCE,
            expires_at=expires_at,
            current_slot="B",
            current_sha256="a" * 64,
            current_generation=7,
            node_id="spk_" + "a" * 32,
            attempt=True,
            claim_deadline=expires_at,
            now=NOW,
        )


def test_published_release_source_verifies_real_signed_tuf_repository(
    tmp_path: Path,
) -> None:
    metadata_root = tmp_path / "publication/metadata"
    target_root = tmp_path / "publication/targets"
    metadata_root.mkdir(parents=True)
    target_root.mkdir(parents=True)
    expiry = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    signers = {
        role: CryptoSigner.generate_ed25519()
        for role in ("root", "timestamp", "snapshot", "targets")
    }
    root = Root(expires=expiry, consistent_snapshot=True)
    for role, signer in signers.items():
        root.add_key(signer.public_key, role)
    root_metadata = Metadata(root)
    root_metadata.sign(signers["root"])
    bootstrap = root_metadata.to_bytes()
    target_bytes = _platform_manifest()
    target_name = _target_name(target_bytes)
    target = TargetFile.from_data(target_name, target_bytes, ["sha256"])
    targets = Metadata(
        Targets(version=7, expires=expiry, targets={target_name: target})
    )
    targets.sign(signers["targets"])
    targets_bytes = targets.to_bytes()
    snapshot = Metadata(
        Snapshot(
            version=7,
            expires=expiry,
            meta={"targets.json": MetaFile.from_data(7, targets_bytes, ["sha256"])},
        )
    )
    snapshot.sign(signers["snapshot"])
    snapshot_bytes = snapshot.to_bytes()
    timestamp = Metadata(
        Timestamp(
            version=7,
            expires=expiry,
            snapshot_meta=MetaFile.from_data(7, snapshot_bytes, ["sha256"]),
        )
    )
    timestamp.sign(signers["timestamp"])
    for name, raw in {
        "timestamp.json": timestamp.to_bytes(),
        "7.snapshot.json": snapshot_bytes,
        "7.targets.json": targets_bytes,
    }.items():
        (metadata_root / name).write_bytes(raw)
    target_sha256 = hashlib.sha256(target_bytes).hexdigest()
    target_directory, target_basename = target_name.rsplit("/", 1)
    target_path = (
        target_root / target_directory / f"{target_sha256}.{target_basename}"
    )
    target_path.parent.mkdir(parents=True)
    target_path.write_bytes(target_bytes)
    source = PublishedTUFReleaseSource(
        publication_metadata_root=metadata_root,
        publication_target_root=target_root,
        verified_metadata_root=tmp_path / "verified/metadata",
        verified_target_root=tmp_path / "verified/targets",
        bootstrap_root=bootstrap,
    )

    verified, version = source.refresh(target_name)

    assert verified == target_bytes
    assert version == 7


@pytest.mark.parametrize(
    "target_name",
    (
        "platform-release.json",
        "platform/releases/latest/" + "a" * 64 + ".json",
        "platform/releases/1.2.3/" + "a" * 63 + ".json",
        "platform/releases/1.2.3/../../targets.json",
    ),
)
def test_published_release_source_rejects_target_aliases_before_fetch(
    tmp_path: Path, target_name: str
) -> None:
    source = PublishedTUFReleaseSource(
        publication_metadata_root=tmp_path / "publication/metadata",
        publication_target_root=tmp_path / "publication/targets",
        verified_metadata_root=tmp_path / "verified/metadata",
        verified_target_root=tmp_path / "verified/targets",
        bootstrap_root=b"not-used-for-invalid-name",
    )

    with pytest.raises(UpdateAuthorizationError, match="target name"):
        source.refresh(target_name)


def test_published_tuf_fetch_rejects_symlinked_nested_target_directory(
    tmp_path: Path,
) -> None:
    metadata_root = tmp_path / "metadata"
    target_root = tmp_path / "targets"
    outside = tmp_path / "outside"
    metadata_root.mkdir()
    target_root.mkdir()
    target_name = f"platform/releases/1.2.3/{'a' * 64}.json"
    outside_target = outside / "releases/1.2.3" / f"{'a' * 64}.json"
    outside_target.parent.mkdir(parents=True)
    outside_target.write_bytes(b"outside-target")
    (target_root / "platform").symlink_to(outside)
    fetcher = _PublishedTufFetcher(metadata_root, target_root)

    with pytest.raises(UpdateAuthorizationError, match="unsafe|unavailable"):
        b"".join(
            fetcher.fetch(
                "https://control.invalid/platform/targets/" + target_name
            )
        )


@pytest.mark.parametrize(
    "target_name",
    (
        lambda: _target_name(version="1.2.2"),
        lambda: f"platform/releases/1.2.3/{'0' * 64}.json",
    ),
)
def test_authority_rejects_target_name_version_or_sha_disagreement(
    tmp_path: Path, target_name
) -> None:
    authority, _ = _authority(tmp_path)

    with pytest.raises(UpdateAuthorizationError, match="target identity"):
        authority.refresh_and_validate(_payload(), target_name=target_name())


def test_public_trust_root_snapshot_rejects_symlink_and_writable_file(
    tmp_path: Path,
) -> None:
    trusted = tmp_path / "root.json"
    trusted.write_bytes(b'{"signed":{},"signatures":[]}\n')
    trusted.chmod(0o644)
    linked = tmp_path / "linked-root.json"
    linked.symlink_to(trusted)

    assert snapshot_public_trust_root(trusted) == trusted.read_bytes()
    with pytest.raises(UpdateAuthorizationError, match="unavailable"):
        snapshot_public_trust_root(linked)
    trusted.chmod(0o664)
    with pytest.raises(UpdateAuthorizationError, match="unsafe"):
        snapshot_public_trust_root(trusted)
