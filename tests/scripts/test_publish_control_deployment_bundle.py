from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from cluster_profiles.deployment_bundle import build_deployment_bundle

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/publish-control-deployment-bundle"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
LAYER_MEDIA_TYPE = "application/vnd.vonk-forge.control-deployment.v1.tar"
CONFIG_MEDIA_TYPE = "application/vnd.oci.empty.v1+json"
ARTIFACT_TYPE = "application/vnd.vonk-forge.control-deployment.v1"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _artifact(name: str, digest: str) -> dict[str, object]:
    return {
        "name": name,
        "reference": f"ghcr.io/example/vonk-forge/{name}@sha256:{digest}",
        "sha256": digest,
        "size": 1024,
        "sbom_sha256": SHA_D,
        "provenance_sha256": SHA_E,
    }


def _bundle_descriptor(raw: bytes) -> dict[str, object]:
    config = b"{}"
    manifest = json.dumps(
        {
            "artifactType": ARTIFACT_TYPE,
            "config": {
                "digest": f"sha256:{_sha256(config)}",
                "mediaType": CONFIG_MEDIA_TYPE,
                "size": len(config),
            },
            "layers": [{
                "digest": f"sha256:{_sha256(raw)}",
                "mediaType": LAYER_MEDIA_TYPE,
                "size": len(raw),
            }],
            "mediaType": MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = _sha256(manifest)
    return {
        "reference": f"ghcr.io/example/vonk-forge/control-deployment@sha256:{digest}",
        "manifest_digest": f"sha256:{digest}",
        "manifest_size": len(manifest),
        "manifest_media_type": MANIFEST_MEDIA_TYPE,
        "layer_digest": f"sha256:{_sha256(raw)}",
        "layer_size": len(raw),
        "layer_media_type": LAYER_MEDIA_TYPE,
    }


def _release(bundle: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 2,
        "platform_version": "1.2.0",
        "build_digest": f"sha256:{SHA_A}",
        "deployment_bundle": bundle,
        "control": {
            "config_version": 3,
            "protocol": {"minimum": 2, "maximum": 3},
            "images": {
                "api": _artifact("api", SHA_A),
                "worker": _artifact("worker", SHA_B),
            },
            "assets": [_artifact("web", SHA_C)],
        },
        "database": {"revision": "0001_fleet_library_baseline"},
        "agent_packages": [
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
        ],
    }


def _write_release(tmp_path: Path, bundle: bytes) -> tuple[Path, Path]:
    bundle_path = tmp_path / "control-deployment.tar"
    bundle_path.write_bytes(bundle)
    release_path = tmp_path / "release.json"
    release_path.write_text(
        json.dumps(_release(_bundle_descriptor(bundle)), sort_keys=True, separators=(",", ":")) + "\n"
    )
    return release_path, bundle_path


def _run(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCRIPT, *arguments],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def canonical_bundle() -> bytes:
    return build_deployment_bundle(ROOT / "deploy/compose")


def test_describe_emits_exact_deterministic_oci_descriptor(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    bundle = tmp_path / "bundle.tar"
    bundle.write_bytes(canonical_bundle)
    result = _run(
        "describe", "--bundle", str(bundle),
        "--repository", "ghcr.io/example/vonk-forge/control-deployment",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == _bundle_descriptor(canonical_bundle)


def test_publish_uploads_only_exact_oci_bundle(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    release, bundle = _write_release(tmp_path, canonical_bundle)
    log = tmp_path / "oras.jsonl"
    oras = tmp_path / "oras"
    oras.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    oras.chmod(0o755)
    result = _run(
        "publish", "--manifest", str(release), "--bundle", str(bundle),
        env=os.environ | {"VONK_ORAS_BIN": str(oras)},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "bundle": _bundle_descriptor(canonical_bundle),
        "schema_version": 1,
    }
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call[:2] for call in calls] == [
        ["blob", "push"], ["blob", "push"], ["manifest", "push"]
    ]


def test_publish_rejects_bundle_descriptor_mismatch_before_upload(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    release, bundle = _write_release(tmp_path, canonical_bundle)
    bundle.write_bytes(canonical_bundle[:-1] + b"X")
    result = _run(
        "publish", "--manifest", str(release), "--bundle", str(bundle),
        env=os.environ | {"VONK_ORAS_BIN": "/bin/false"},
    )
    assert result.returncode == 2
    assert "descriptor" in result.stderr
