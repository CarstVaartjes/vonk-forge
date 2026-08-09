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
    PlatformIdentity,
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


def _payload(name: str, digest: str, size: int = 4096) -> dict[str, object]:
    return {"name": name, "sha256": digest, "size": size}


def _manifest() -> dict[str, object]:
    return {
        "schema_version": 2,
        "platform_version": "1.2.0",
        "build_digest": f"sha256:{SHA_A}",
        "host_updater_abi": {"minimum": 2, "maximum": 3},
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
            "expand_revision": "0010_update_rollouts",
            "contract_revision": None,
            "predecessor_compatible": True,
        },
        "agents": [
            {
                "architecture": "linux-arm64",
                "protocol": {"minimum": 1, "maximum": 2},
                "artifact": _artifact("agent-linux-arm64", SHA_A),
                "payload": _payload("vonk-agent", SHA_B),
            }
        ],
        "supervisors": [
            {
                "architecture": "linux-arm64",
                "artifact": _artifact("supervisor-linux-arm64", SHA_B),
                "payload": _payload("vonk-agent-supervisor", SHA_C, 8192),
            }
        ],
        "tooling": [
            {
                "architecture": "linux-arm64",
                "artifact": _artifact("tooling-linux-arm64", SHA_C),
                "payload": _payload("vonk-forge-tooling", SHA_D, 16384),
            }
        ],
        "rollback": {
            "predecessors": [
                {
                    "target_name": f"platform/releases/1.1.0/{SHA_B}.json",
                    "target_sha256": SHA_B,
                    "release_digest": f"sha256:{SHA_C}",
                    "build_digest": f"sha256:{SHA_B}",
                    "deployment_bundle_digest": f"sha256:{SHA_D}",
                }
            ],
        },
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
    assert release.host_updater_abi.minimum == 2
    assert release.host_updater_abi.maximum == 3
    assert release.control.config_version == 3
    assert release.agent_for("linux-arm64").protocol.minimum == 1
    assert release.digest.startswith("sha256:")
    assert len(release.digest) == 71


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
        }
    ]

    release = PlatformRelease.from_bytes(
        (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )

    assert release.agent_packages[0].architecture == "linux-arm64"
    assert release.agent_packages[0].version == release.platform_version
    assert release.agent_packages[0].filename.endswith("_arm64.deb")


def test_platform_release_loads_exact_oci_bundle_and_predecessor_contract() -> None:
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
    assert release.predecessors == (
        platform_release_module.AuthorizedPredecessor(
            target_name=f"platform/releases/1.1.0/{SHA_B}.json",
            target_sha256=SHA_B,
            release_digest=f"sha256:{SHA_C}",
            build_digest=f"sha256:{SHA_B}",
            deployment_bundle_digest=f"sha256:{SHA_D}",
        ),
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


def test_architecture_artifact_keeps_oci_and_installed_payload_metadata_distinct() -> (
    None
):
    raw = (json.dumps(_manifest(), sort_keys=True) + "\n").encode()

    agent = PlatformRelease.from_bytes(raw).agent_for("linux-arm64")

    assert agent.artifact.name == "agent-linux-arm64"
    assert agent.artifact.sha256 == SHA_A
    assert agent.artifact.size == 1024
    assert agent.payload_name == "vonk-agent"
    assert agent.payload_sha256 == SHA_B
    assert agent.payload_size == 4096


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


def test_platform_release_digest_binds_installed_payload_metadata(
    tmp_path: Path,
) -> None:
    original = _manifest()
    changed = copy.deepcopy(original)
    changed["agents"][0]["payload"]["sha256"] = SHA_C  # type: ignore[index]

    first = PlatformRelease.load(_write(tmp_path, original, "first.json"))
    second = PlatformRelease.load(_write(tmp_path, changed, "second.json"))

    assert first.agents[0].artifact == second.agents[0].artifact
    assert first.digest != second.digest


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=True),
        lambda d: d.update(platform_version="v1.2"),
        lambda d: d.update(build_digest=SHA_A),
        lambda d: d.update(schema_version=1),
        lambda d: d["host_updater_abi"].update(minimum=4, maximum=3),  # type: ignore[index,union-attr]
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
        lambda d: d["agents"].append(copy.deepcopy(d["agents"][0])),  # type: ignore[union-attr,index]
        lambda d: d["agents"][0].pop("payload"),  # type: ignore[index,union-attr]
        lambda d: d["supervisors"][0].pop("payload"),  # type: ignore[index,union-attr]
        lambda d: d["tooling"][0].pop("payload"),  # type: ignore[index,union-attr]
        lambda d: d["agents"][0]["payload"].pop("name"),  # type: ignore[index,union-attr]
        lambda d: d["agents"][0]["payload"].pop("sha256"),  # type: ignore[index,union-attr]
        lambda d: d["agents"][0]["payload"].pop("size"),  # type: ignore[index,union-attr]
        lambda d: d["agents"][0]["payload"].update(extra=True),  # type: ignore[index,union-attr]
        lambda d: d["agents"][0]["payload"].update(name="Vonk Forge Agent"),  # type: ignore[index,union-attr]
        lambda d: d["agents"][0]["payload"].update(sha256=f"sha256:{SHA_B}"),  # type: ignore[index,union-attr]
        lambda d: d["agents"][0]["payload"].update(size=63),  # type: ignore[index,union-attr]
        lambda d: d["agents"][0]["payload"].update(size=268435457),  # type: ignore[index,union-attr]
        lambda d: d["database"].update(  # type: ignore[union-attr]
            contract_revision="0012_contract", predecessor_compatible=False
        ),
        lambda d: d["rollback"].update(  # type: ignore[union-attr]
            compatible_predecessor_builds=[f"sha256:{SHA_B}"]
        ),
        lambda d: d["rollback"]["predecessors"][0].update(  # type: ignore[index]
            target_name="platform-release.json"
        ),
        lambda d: d["rollback"]["predecessors"][0].update(  # type: ignore[index]
            target_name=f"platform/releases/1.1.0/{SHA_C}.json"
        ),
        lambda d: d["rollback"]["predecessors"].append(  # type: ignore[index,union-attr]
            {
                **copy.deepcopy(d["rollback"]["predecessors"][0]),  # type: ignore[index]
                "build_digest": f"sha256:{SHA_E}",
            }
        ),
    ],
    ids=[
        "unknown-field",
        "invalid-semver",
        "invalid-build-digest",
        "legacy-schema-version",
        "invalid-host-updater-abi-range",
        "bundle-reference-manifest-digest-mismatch",
        "bundle-reference-parent-traversal",
        "artifact-reference-empty-component",
        "bundle-manifest-size-zero",
        "floating-image",
        "missing-sbom",
        "invalid-protocol-range",
        "overlapping-architecture",
        "agent-missing-payload",
        "supervisor-missing-payload",
        "tooling-missing-payload",
        "payload-missing-name",
        "payload-missing-sha256",
        "payload-missing-size",
        "payload-unknown-field",
        "payload-invalid-name",
        "payload-prefixed-digest",
        "payload-too-small",
        "payload-exceeds-supervisor-limit",
        "destructive-migration-without-predecessor-compatibility",
        "legacy-compatible-build-only-rollback",
        "predecessor-target-alias",
        "predecessor-target-name-sha-disagreement",
        "duplicate-predecessor-target",
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


def test_compatibility_requires_architecture_protocol_overlap_and_rollback(
    tmp_path: Path,
) -> None:
    release = PlatformRelease.load(_write(tmp_path, _manifest()))
    compatible = PlatformIdentity(
        platform_version="1.1.0",
        platform_target_name=f"platform/releases/1.1.0/{SHA_B}.json",
        platform_target_sha256=SHA_B,
        release_digest=f"sha256:{SHA_C}",
        build_digest=f"sha256:{SHA_B}",
        deployment_bundle_digest=f"sha256:{SHA_D}",
        architecture="linux-arm64",
        control_api_protocol=2,
        agent_protocol=1,
    )

    report = release.compatibility(compatible)

    assert report.compatible is True
    assert report.update_recommended is True
    assert report.reasons == ()

    incompatible = PlatformIdentity(
        platform_version="1.1.0",
        platform_target_name=f"platform/releases/1.1.0/{SHA_C}.json",
        platform_target_sha256=SHA_C,
        release_digest=f"sha256:{SHA_C}",
        build_digest=f"sha256:{SHA_C}",
        deployment_bundle_digest=f"sha256:{SHA_D}",
        architecture="linux-x86_64",
        control_api_protocol=1,
        agent_protocol=7,
    )
    rejected = release.compatibility(incompatible)
    assert rejected.compatible is False
    assert set(rejected.reasons) == {
        "architecture-not-published",
        "control-protocol-incompatible",
        "predecessor-not-recovery-compatible",
    }


@pytest.mark.parametrize(
    "changes",
    (
        {
            "platform_version": "1.0.0",
            "platform_target_name": f"platform/releases/1.0.0/{SHA_B}.json",
        },
        {
            "platform_target_name": f"platform/releases/1.1.0/{SHA_E}.json",
            "platform_target_sha256": SHA_E,
        },
        {"release_digest": f"sha256:{SHA_E}"},
        {"deployment_bundle_digest": f"sha256:{SHA_E}"},
    ),
    ids=("version", "target", "release", "bundle"),
)
def test_compatibility_requires_one_complete_exact_predecessor_descriptor(
    tmp_path: Path, changes: dict[str, str]
) -> None:
    release = PlatformRelease.load(_write(tmp_path, _manifest()))
    identity = {
        "platform_version": "1.1.0",
        "platform_target_name": f"platform/releases/1.1.0/{SHA_B}.json",
        "platform_target_sha256": SHA_B,
        "release_digest": f"sha256:{SHA_C}",
        "build_digest": f"sha256:{SHA_B}",
        "deployment_bundle_digest": f"sha256:{SHA_D}",
        "architecture": "linux-arm64",
        "control_api_protocol": 2,
        "agent_protocol": 1,
    }
    identity.update(changes)

    report = release.compatibility(PlatformIdentity(**identity))

    assert report.compatible is False
    assert "predecessor-not-recovery-compatible" in report.reasons


def test_initial_platform_release_allows_no_predecessor(tmp_path: Path) -> None:
    document = _manifest()
    document["rollback"]["predecessors"] = []  # type: ignore[index]

    release = PlatformRelease.load(_write(tmp_path, document))

    assert release.predecessors == ()


def test_same_build_is_current_and_not_an_update(tmp_path: Path) -> None:
    release = PlatformRelease.load(_write(tmp_path, _manifest()))
    current = PlatformIdentity(
        platform_version="1.2.0",
        platform_target_name=f"platform/releases/1.2.0/{SHA_E}.json",
        platform_target_sha256=SHA_E,
        release_digest=release.digest,
        build_digest=f"sha256:{SHA_A}",
        deployment_bundle_digest=release.deployment_bundle.manifest_digest,
        architecture="linux-arm64",
        control_api_protocol=2,
        agent_protocol=1,
    )

    report = release.compatibility(current)

    assert report.compatible is True
    assert report.update_recommended is False


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
