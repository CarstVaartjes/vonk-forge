from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from jsonschema import Draft202012Validator
from securesystemslib.signer import CryptoSigner
from tuf.api.exceptions import DownloadHTTPError
from tuf.api.metadata import (
    Metadata,
    MetaFile,
    Root,
    Snapshot,
    TargetFile,
    Targets,
    Timestamp,
)
from tuf.ngclient import FetcherInterface

from cluster_profiles import platform_release as platform_release_module
from cluster_profiles.platform_release import (
    PlatformRelease,
    PlatformReleaseError,
)
from cluster_profiles.update_trust import UpdateTrust, UpdateTrustError

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64


def _artifact(name: str, digest: str) -> dict[str, object]:
    return {
        "name": name,
        "reference": f"ghcr.io/example/vonk-forge/{name}@sha256:{digest}",
        "sha256": digest,
        "size": 1024,
        "sbom_sha256": SHA_D,
        "provenance_sha256": SHA_E,
    }


def _agent_package(
    architecture: str, deb_architecture: str, digest: str
) -> dict[str, object]:
    return {
        "architecture": architecture,
        "name": "vonk-forge-agent",
        "version": "1.2.0",
        "filename": f"vonk-forge-agent_1.2.0_{deb_architecture}.deb",
        "sha256": digest,
        "size": 4096,
        "sbom_sha256": SHA_B,
        "provenance_sha256": SHA_C,
        "sigstore_bundle_sha256": SHA_D,
    }


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 2,
        "platform_version": "1.2.0",
        "build_digest": f"sha256:{SHA_A}",
        "deployment_bundle": {
            "reference": (
                f"ghcr.io/example/vonk-forge/control-deployment@sha256:{SHA_A}"
            ),
            "manifest_digest": f"sha256:{SHA_A}",
            "manifest_size": 4096,
            "manifest_media_type": "application/vnd.oci.image.manifest.v1+json",
            "layer_digest": f"sha256:{SHA_B}",
            "layer_size": 1048576,
            "layer_media_type": "application/vnd.vonk-forge.control-deployment.v1.tar",
        },
        "control": {
            "config_version": 3,
            "protocol": {"minimum": 2, "maximum": 3},
            "images": {
                "api": _artifact("api", SHA_A),
                "worker": _artifact("worker", SHA_B),
            },
            "assets": [_artifact("web", SHA_C)],
        },
        "database": {
            "revision": "0001_fleet_library_baseline",
        },
        "agent_packages": [
            _agent_package("linux-arm64", "arm64", SHA_A),
            _agent_package("linux-amd64", "amd64", SHA_B),
        ],
    }


def _write(
    tmp_path: Path, document: dict[str, object], name: str = "release.json"
) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_platform_release_loads_strict_typed_contract(tmp_path: Path) -> None:
    release = PlatformRelease.load(_write(tmp_path, _manifest()))

    assert release.platform_version == "1.2.0"
    assert release.build_digest == f"sha256:{SHA_A}"
    assert release.control.config_version == 3
    assert {package.architecture for package in release.agent_packages} == {
        "linux-amd64",
        "linux-arm64",
    }
    assert release.digest.startswith("sha256:")
    assert len(release.digest) == 71


def test_platform_release_rejects_legacy_supervisor_metadata() -> None:
    document = _manifest()
    document["supervisors"] = []

    with pytest.raises(PlatformReleaseError, match="supervisors"):
        PlatformRelease.from_bytes(json.dumps(document).encode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("host_updater_abi", {"minimum": 2, "maximum": 3}),
        ("agents", []),
        ("tooling", []),
        ("rollback", {"predecessors": []}),
    ],
)
def test_platform_release_rejects_obsolete_host_rollout_metadata(
    field: str, value: object
) -> None:
    document = _manifest()
    document[field] = value

    with pytest.raises(PlatformReleaseError, match=field):
        PlatformRelease.from_bytes(json.dumps(document).encode())


@pytest.mark.parametrize(
    "field,value",
    [
        ("expand_revision", "0001_fleet_library_baseline"),
        ("contract_revision", None),
        ("predecessor_compatible", True),
    ],
)
def test_platform_release_rejects_migration_compatibility_metadata(
    field: str, value: object
) -> None:
    document = _manifest()
    document["database"][field] = value  # type: ignore[index]

    with pytest.raises(PlatformReleaseError, match=field):
        PlatformRelease.from_bytes(json.dumps(document).encode())


def test_platform_release_requires_public_agent_package_metadata() -> None:
    document = _manifest()
    del document["agent_packages"]

    with pytest.raises(PlatformReleaseError, match="agent_packages"):
        PlatformRelease.from_bytes(json.dumps(document).encode())


def test_platform_release_reuses_checked_schema_and_rejects_invalid_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = getattr(platform_release_module, "_validator", None)
    if validator is not None:
        validator.cache_clear()

    schemas: set[int] = set()
    load_schema = platform_release_module._schema

    def track_schema() -> dict:
        schema = load_schema()
        schemas.add(id(schema))
        return schema

    checked = 0
    check_schema = Draft202012Validator.check_schema

    def count_checked_schema(cls, schema: dict, *args: object, **kwargs: object) -> None:
        nonlocal checked
        assert id(schema) in schemas
        checked += 1
        check_schema(schema, *args, **kwargs)

    monkeypatch.setattr(platform_release_module, "_schema", track_schema)
    monkeypatch.setattr(
        Draft202012Validator, "check_schema", classmethod(count_checked_schema)
    )

    raw = json.dumps(_manifest(), sort_keys=True, separators=(",", ":")).encode()
    PlatformRelease.from_bytes(raw)
    PlatformRelease.from_bytes(raw)

    assert checked == 1

    invalid = _manifest()
    invalid["unexpected"] = True
    with pytest.raises(PlatformReleaseError, match="unexpected"):
        PlatformRelease.from_bytes(json.dumps(invalid).encode())


def test_platform_release_exposes_version_bound_agent_debian_package() -> None:
    document = _manifest()
    document["agent_packages"] = [
        {
            "architecture": "linux-arm64",
            "name": "vonk-forge-agent",
            "version": "1.2.0",
            "filename": "vonk-forge-agent_1.2.0_arm64.deb",
            "sha256": SHA_A,
            "size": 4096,
            "sbom_sha256": SHA_B,
            "provenance_sha256": SHA_C,
            "sigstore_bundle_sha256": SHA_D,
        },
        {
            "architecture": "linux-amd64",
            "name": "vonk-forge-agent",
            "version": "1.2.0",
            "filename": "vonk-forge-agent_1.2.0_amd64.deb",
            "sha256": SHA_B,
            "size": 4096,
            "sbom_sha256": SHA_C,
            "provenance_sha256": SHA_D,
            "sigstore_bundle_sha256": SHA_E,
        },
    ]

    release = PlatformRelease.from_bytes(
        (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )

    assert release.agent_packages[0].architecture == "linux-arm64"
    assert release.agent_packages[0].version == release.platform_version
    assert release.agent_packages[0].filename.endswith("_arm64.deb")
    assert release.agent_packages[1].architecture == "linux-amd64"
    assert release.agent_packages[1].filename.endswith("_amd64.deb")


def test_platform_release_requires_both_exact_agent_package_architectures() -> None:
    document = _manifest()
    document["agent_packages"] = [
        {
            "architecture": "linux-arm64",
            "name": "vonk-forge-agent",
            "version": "1.2.0",
            "filename": "vonk-forge-agent_1.2.0_arm64.deb",
            "sha256": SHA_A,
            "size": 4096,
            "sbom_sha256": SHA_B,
            "provenance_sha256": SHA_C,
            "sigstore_bundle_sha256": SHA_D,
        }
    ]

    with pytest.raises(PlatformReleaseError, match="agent_packages"):
        PlatformRelease.from_bytes(json.dumps(document).encode())

    document["agent_packages"].append(
        {
            **document["agent_packages"][0],
            "architecture": "linux-amd64",
            "filename": "vonk-forge-agent_1.2.0_arm64.deb",
        }
    )
    with pytest.raises(PlatformReleaseError, match="filename"):
        PlatformRelease.from_bytes(json.dumps(document).encode())


def test_platform_release_loads_exact_oci_bundle_contract() -> None:
    release = PlatformRelease.from_bytes(
        (json.dumps(_manifest(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    )

    assert release.deployment_bundle == platform_release_module.OciDeploymentBundle(
        reference=f"ghcr.io/example/vonk-forge/control-deployment@sha256:{SHA_A}",
        manifest_digest=f"sha256:{SHA_A}",
        manifest_size=4096,
        manifest_media_type="application/vnd.oci.image.manifest.v1+json",
        layer_digest=f"sha256:{SHA_B}",
        layer_size=1048576,
        layer_media_type="application/vnd.vonk-forge.control-deployment.v1.tar",
    )


def test_platform_release_validates_external_versioned_target_identity() -> None:
    raw = (
        json.dumps(_manifest(), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    release = PlatformRelease.from_bytes(raw)
    target_sha256 = __import__("hashlib").sha256(raw).hexdigest()

    release.validate_target_identity(
        f"platform/releases/1.2.0/{target_sha256}.json",
        target_sha256,
    )

    for target_name, trusted_sha256 in (
        ("platform-release.json", target_sha256),
        (f"platform/releases/latest/{target_sha256}.json", target_sha256),
        (f"platform/releases/1.1.0/{target_sha256}.json", target_sha256),
        (f"platform/releases/1.2.0/{SHA_E}.json", target_sha256),
    ):
        with pytest.raises(PlatformReleaseError, match="target identity"):
            release.validate_target_identity(target_name, trusted_sha256)


def test_target_name_sha_is_tuf_byte_digest_not_manifest_self_reference() -> None:
    first = (
        json.dumps(_manifest(), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    second = json.dumps(_manifest(), indent=2).encode()
    first_release = PlatformRelease.from_bytes(first)
    second_release = PlatformRelease.from_bytes(second)

    assert first_release.digest == second_release.digest
    assert (
        __import__("hashlib").sha256(first).digest()
        != __import__("hashlib").sha256(second).digest()
    )


@pytest.mark.parametrize(
    "raw",
    [json.dumps(_manifest()), bytearray(json.dumps(_manifest()).encode())],
    ids=["text", "mutable-bytes"],
)
def test_platform_release_from_bytes_requires_immutable_bytes(raw: object) -> None:
    with pytest.raises(PlatformReleaseError, match="must be bytes"):
        PlatformRelease.from_bytes(raw)  # type: ignore[arg-type]


def test_platform_update_schema_is_packaged_and_matches_repository_copy() -> None:
    repository = (
        Path(__file__).resolve().parents[2]
        / "schemas/platform-update-manifest.schema.json"
    ).read_bytes()
    packaged = (
        resources.files("cluster_profiles")
        .joinpath("schemas", "platform-update-manifest.schema.json")
        .read_bytes()
    )

    assert packaged == repository


def test_platform_release_digest_is_canonical_under_object_key_reordering(
    tmp_path: Path,
) -> None:
    original = _manifest()
    reordered = dict(reversed(list(original.items())))
    reordered["control"] = dict(
        reversed(list(copy.deepcopy(original["control"]).items()))  # type: ignore[union-attr]
    )

    first = PlatformRelease.load(_write(tmp_path, original, "first.json"))
    second = PlatformRelease.load(_write(tmp_path, reordered, "second.json"))

    assert first.digest == second.digest


def test_platform_release_digest_binds_agent_package_metadata(
    tmp_path: Path,
) -> None:
    original = _manifest()
    changed = copy.deepcopy(original)
    changed["agent_packages"][0]["sha256"] = SHA_E  # type: ignore[index]

    first = PlatformRelease.load(_write(tmp_path, original, "first.json"))
    second = PlatformRelease.load(_write(tmp_path, changed, "second.json"))

    assert first.agent_packages[1] == second.agent_packages[1]
    assert first.digest != second.digest


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=True),
        lambda d: d.update(platform_version="v1.2"),
        lambda d: d.update(build_digest=SHA_A),
        lambda d: d.update(schema_version=1),
        lambda d: d["deployment_bundle"].update(  # type: ignore[index,union-attr]
            reference=f"ghcr.io/example/bundle@sha256:{SHA_C}"
        ),
        lambda d: d["deployment_bundle"].update(  # type: ignore[index,union-attr]
            reference=f"ghcr.io/a/../../evil@sha256:{SHA_A}"
        ),
        lambda d: d["control"]["images"]["api"].update(  # type: ignore[index,union-attr]
            reference=f"ghcr.io/a//api@sha256:{SHA_A}"
        ),
        lambda d: d["deployment_bundle"].update(manifest_size=0),  # type: ignore[index,union-attr]
        lambda d: d["control"]["images"]["api"].update(  # type: ignore[index,union-attr]
            reference="ghcr.io/example/vonk-forge/api:latest"
        ),
        lambda d: d["control"]["images"]["api"].update(  # type: ignore[index,union-attr]
            sbom_sha256=None
        ),
        lambda d: d["control"].update(protocol={"minimum": 3, "maximum": 2}),  # type: ignore[union-attr]
    ],
    ids=[
        "unknown-field",
        "invalid-semver",
        "invalid-build-digest",
        "legacy-schema-version",
        "bundle-reference-manifest-digest-mismatch",
        "bundle-reference-parent-traversal",
        "artifact-reference-empty-component",
        "bundle-manifest-size-zero",
        "floating-image",
        "missing-sbom",
        "invalid-protocol-range",
    ],
)
def test_platform_release_rejects_unsafe_or_ambiguous_inputs(
    tmp_path: Path, mutate: object
) -> None:
    document = _manifest()
    mutate(document)  # type: ignore[operator]

    with pytest.raises(PlatformReleaseError):
        PlatformRelease.load(_write(tmp_path, document))


def test_artifact_reference_digest_must_match_bound_sha256(tmp_path: Path) -> None:
    document = _manifest()
    document["control"]["images"]["api"]["sha256"] = SHA_C  # type: ignore[index]

    with pytest.raises(PlatformReleaseError, match="reference digest"):
        PlatformRelease.load(_write(tmp_path, document))


class _RepositoryFetcher(FetcherInterface):
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.urls: list[str] = []

    def _fetch(self, url: str):
        self.urls.append(url)
        path = urlsplit(url).path
        name = (
            path.split("/platform/targets/", 1)[1]
            if "/platform/targets/" in path
            else path.rsplit("/", 1)[-1]
        )
        try:
            yield self.files[name]
        except KeyError as error:
            raise DownloadHTTPError("missing", 404) from error


def _signed_repository(
    target_bytes: bytes,
    *,
    expired: bool = False,
    version: int = 1,
    signers: dict[str, CryptoSigner] | None = None,
    root_bytes: bytes | None = None,
) -> tuple[bytes, _RepositoryFetcher]:
    expiry = datetime.now(UTC) + (-timedelta(days=1) if expired else timedelta(days=1))
    signers = signers or {
        role: CryptoSigner.generate_ed25519()
        for role in ("root", "timestamp", "snapshot", "targets")
    }
    if root_bytes is None:
        root = Root(
            expires=datetime.now(UTC) + timedelta(days=1),
            consistent_snapshot=True,
        )
        for role, signer in signers.items():
            root.add_key(signer.public_key, role)
        root_metadata = Metadata(root)
        root_metadata.sign(signers["root"])
        root_bytes = root_metadata.to_bytes()
    target_sha256 = __import__("hashlib").sha256(target_bytes).hexdigest()
    target_name = f"platform/releases/1.2.0/{target_sha256}.json"
    target = TargetFile.from_data(target_name, target_bytes, ["sha256"])
    targets = Metadata(
        Targets(
            version=version,
            expires=expiry,
            targets={target_name: target},
        )
    )
    targets.sign(signers["targets"])
    targets_bytes = targets.to_bytes()
    snapshot = Metadata(
        Snapshot(
            version=version,
            expires=expiry,
            meta={
                "targets.json": MetaFile.from_data(version, targets_bytes, ["sha256"])
            },
        )
    )
    snapshot.sign(signers["snapshot"])
    snapshot_bytes = snapshot.to_bytes()
    timestamp = Metadata(
        Timestamp(
            version=version,
            expires=expiry,
            snapshot_meta=MetaFile.from_data(version, snapshot_bytes, ["sha256"]),
        )
    )
    timestamp.sign(signers["timestamp"])
    fetcher = _RepositoryFetcher(
        {
            "timestamp.json": timestamp.to_bytes(),
            f"{version}.snapshot.json": snapshot_bytes,
            f"{version}.targets.json": targets_bytes,
            target_name.rsplit("/", 1)[0]
            + "/"
            + target_sha256
            + "."
            + target_name.rsplit("/", 1)[1]: target_bytes,
        }
    )
    fetcher.signers = signers
    return root_bytes, fetcher


def _rotated_repository(target_bytes: bytes) -> tuple[bytes, _RepositoryFetcher]:
    expiry = datetime.now(UTC) + timedelta(days=1)
    old_root = CryptoSigner.generate_ed25519()
    new_root = CryptoSigner.generate_ed25519()
    signers = {
        role: CryptoSigner.generate_ed25519()
        for role in ("timestamp", "snapshot", "targets")
    }
    root1 = Root(version=1, expires=expiry, consistent_snapshot=True)
    root1.add_key(old_root.public_key, "root")
    for role, signer in signers.items():
        root1.add_key(signer.public_key, role)
    root1_metadata = Metadata(root1)
    root1_metadata.sign(old_root)
    root1_bytes = root1_metadata.to_bytes()

    root2 = Root(version=2, expires=expiry, consistent_snapshot=True)
    root2.add_key(new_root.public_key, "root")
    for role, signer in signers.items():
        root2.add_key(signer.public_key, role)
    root2_metadata = Metadata(root2)
    root2_metadata.sign(old_root)
    root2_metadata.sign(new_root, append=True)

    _, fetcher = _signed_repository(
        target_bytes,
        version=2,
        signers={"root": new_root, **signers},
        root_bytes=root1_bytes,
    )
    fetcher.files["2.root.json"] = root2_metadata.to_bytes()
    return root1_bytes, fetcher


def _trust(
    tmp_path: Path, root_bytes: bytes, fetcher: _RepositoryFetcher
) -> UpdateTrust:
    return UpdateTrust(
        metadata_root=tmp_path / "metadata",
        target_root=tmp_path / "targets",
        metadata_base_url="https://updates.example.test/platform/metadata/",
        target_base_url="https://updates.example.test/platform/targets/",
        bootstrap_root=root_bytes,
        fetcher=fetcher,
    )


def test_update_trust_refreshes_and_returns_verified_target_bytes(
    tmp_path: Path,
) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    root_bytes, fetcher = _signed_repository(target_bytes)
    trust = _trust(tmp_path, root_bytes, fetcher)

    trust.refresh()
    target_sha256 = __import__("hashlib").sha256(target_bytes).hexdigest()
    target_name = f"platform/releases/1.2.0/{target_sha256}.json"
    target = trust.trusted_target(target_name)

    assert target.data == target_bytes
    assert target.length == len(target_bytes)
    assert target.sha256 == __import__("hashlib").sha256(target_bytes).hexdigest()
    state = json.loads((tmp_path / "metadata/trusted-state.json").read_text())
    assert state == {"root": 1, "snapshot": 1, "targets": 1, "timestamp": 1}


def test_update_trust_atomically_returns_target_with_its_targets_version(
    tmp_path: Path,
) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    root_bytes, fetcher = _signed_repository(target_bytes, version=7)
    trust = _trust(tmp_path, root_bytes, fetcher)
    target_sha256 = __import__("hashlib").sha256(target_bytes).hexdigest()

    target, targets_version = trust.refresh_and_trusted_target(
        f"platform/releases/1.2.0/{target_sha256}.json"
    )

    assert target.data == target_bytes
    assert target.sha256 == target_sha256
    assert targets_version == 7


@pytest.mark.parametrize(
    "target_name",
    (
        "platform-release.json",
        f"platform/releases/latest/{SHA_A}.json",
        f"platform/releases/1.2.0/../{SHA_A}.json",
        f"other/releases/1.2.0/{SHA_A}.json",
    ),
)
def test_update_trust_rejects_nonversioned_target_names(
    tmp_path: Path, target_name: str
) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    root_bytes, fetcher = _signed_repository(target_bytes)
    trust = _trust(tmp_path, root_bytes, fetcher)
    trust.refresh()

    with pytest.raises(UpdateTrustError, match="target name"):
        trust.trusted_target(target_name)


def test_update_trust_accepts_valid_root_rotation_and_persists_new_floor(
    tmp_path: Path,
) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    root_bytes, fetcher = _rotated_repository(target_bytes)
    trust = _trust(tmp_path, root_bytes, fetcher)

    trust.refresh()

    state = json.loads((tmp_path / "metadata/trusted-state.json").read_text())
    assert state["root"] == 2
    assert "https://updates.example.test/platform/metadata/2.root.json" in fetcher.urls


@pytest.mark.parametrize("failure", ["expired", "target-bytes", "snapshot-bytes"])
def test_update_trust_rejects_expiry_and_metadata_or_target_mismatch(
    tmp_path: Path, failure: str
) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    root_bytes, fetcher = _signed_repository(target_bytes, expired=failure == "expired")
    if failure == "target-bytes":
        target_sha256 = __import__("hashlib").sha256(target_bytes).hexdigest()
        fetcher.files[
            f"platform/releases/1.2.0/{target_sha256}.{target_sha256}.json"
        ] = b"tampered"
    if failure == "snapshot-bytes":
        fetcher.files["1.snapshot.json"] = b"tampered"
    trust = _trust(tmp_path, root_bytes, fetcher)

    with pytest.raises(UpdateTrustError):
        trust.refresh()
        target_sha256 = __import__("hashlib").sha256(target_bytes).hexdigest()
        trust.trusted_target(f"platform/releases/1.2.0/{target_sha256}.json")


def test_update_trust_rejects_metadata_version_rollback(tmp_path: Path) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    signers = {
        role: CryptoSigner.generate_ed25519()
        for role in ("root", "timestamp", "snapshot", "targets")
    }
    root_bytes, newer = _signed_repository(target_bytes, version=2, signers=signers)
    trust = _trust(tmp_path, root_bytes, newer)
    trust.refresh()

    _, older = _signed_repository(
        target_bytes, version=1, signers=signers, root_bytes=root_bytes
    )
    replay = _trust(tmp_path, root_bytes, older)

    with pytest.raises(UpdateTrustError):
        replay.refresh()


def test_update_trust_rejects_symlinked_cache_without_mutating_target(
    tmp_path: Path,
) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    root_bytes, fetcher = _signed_repository(target_bytes)
    redirected = tmp_path / "redirected"
    redirected.mkdir(mode=0o755)
    (tmp_path / "metadata").symlink_to(redirected, target_is_directory=True)
    trust = _trust(tmp_path, root_bytes, fetcher)

    with pytest.raises(UpdateTrustError, match="cache directory"):
        trust.refresh()

    assert redirected.stat().st_mode & 0o777 == 0o755
    assert list(redirected.iterdir()) == []


def test_update_trust_recovers_stale_atomic_state_temporary(tmp_path: Path) -> None:
    target_bytes = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()
    root_bytes, fetcher = _signed_repository(target_bytes)
    trust = _trust(tmp_path, root_bytes, fetcher)
    trust.refresh()
    stale = tmp_path / "metadata/.trusted-state.json.new"
    stale.write_text("interrupted", encoding="utf-8")

    trust.refresh()

    assert (
        json.loads((tmp_path / "metadata/trusted-state.json").read_text())["root"] == 1
    )
