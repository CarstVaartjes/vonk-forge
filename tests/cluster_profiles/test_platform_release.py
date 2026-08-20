from __future__ import annotations

import copy
import json
from importlib import resources
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from cluster_profiles import platform_release as platform_release_module
from cluster_profiles.platform_release import (
    PlatformRelease,
    PlatformReleaseError,
)

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


@pytest.mark.parametrize(
    "raw",
    [json.dumps(_manifest()), bytearray(json.dumps(_manifest()).encode())],
    ids=["text", "mutable-bytes"],
)
def test_platform_release_from_bytes_requires_immutable_bytes(raw: object) -> None:
    with pytest.raises(PlatformReleaseError, match="must be bytes"):
        PlatformRelease.from_bytes(raw)  # type: ignore[arg-type]


def test_platform_release_schema_is_packaged_and_matches_repository_copy() -> None:
    repository = (
        Path(__file__).resolve().parents[2]
        / "schemas/platform-release-manifest.schema.json"
    ).read_bytes()
    packaged = (
        resources.files("cluster_profiles")
        .joinpath("schemas", "platform-release-manifest.schema.json")
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
