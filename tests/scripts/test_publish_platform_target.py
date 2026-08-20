from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from cluster_profiles.deployment_bundle import build_deployment_bundle

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/publish-platform-target"
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
    layer_digest = _sha256(raw)
    config = b"{}"
    manifest = json.dumps(
        {
            "artifactType": ARTIFACT_TYPE,
            "config": {
                "digest": f"sha256:{_sha256(config)}",
                "mediaType": CONFIG_MEDIA_TYPE,
                "size": len(config),
            },
            "layers": [
                {
                    "digest": f"sha256:{layer_digest}",
                    "mediaType": LAYER_MEDIA_TYPE,
                    "size": len(raw),
                }
            ],
            "mediaType": MANIFEST_MEDIA_TYPE,
            "schemaVersion": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest_digest = _sha256(manifest)
    return {
        "reference": (
            "ghcr.io/example/vonk-forge/control-deployment"
            f"@sha256:{manifest_digest}"
        ),
        "manifest_digest": f"sha256:{manifest_digest}",
        "manifest_size": len(manifest),
        "manifest_media_type": MANIFEST_MEDIA_TYPE,
        "layer_digest": f"sha256:{layer_digest}",
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
        "database": {
            "revision": "0001_fleet_library_baseline",
        },
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


def _write_release(tmp_path: Path, bundle: bytes) -> tuple[Path, Path, str, str]:
    bundle_path = tmp_path / "control-deployment.tar"
    bundle_path.write_bytes(bundle)
    release_path = tmp_path / "release.json"
    release_raw = (
        json.dumps(
            _release(_bundle_descriptor(bundle)), sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode()
    release_path.write_bytes(release_raw)
    target_sha256 = _sha256(release_raw)
    target_name = f"platform/releases/1.2.0/{target_sha256}.json"
    return release_path, bundle_path, target_name, target_sha256


def _fake_publishers(
    tmp_path: Path,
    *,
    bad_tuf_receipt: bool = False,
    spam_tuf_receipt: bool = False,
) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "publication.jsonl"
    source = """#!/usr/bin/env python3
import json
import hashlib
import os
import sys
from pathlib import Path

name = (
    "tuf-publisher" if sys.argv[1] == "publish-target"
    else "channel-publisher" if sys.argv[1] == "publish-channel"
    else "oras"
)
entry = {"name": name, "argv": sys.argv[1:], "env_keys": sorted(os.environ)}
if name == "channel-publisher":
    document_path = Path(sys.argv[sys.argv.index("--document") + 1])
    entry["document"] = json.loads(document_path.read_text(encoding="utf-8"))
with open(__LOG__, "a", encoding="utf-8") as stream:
    stream.write(json.dumps(entry, sort_keys=True) + "\\n")
if name == "tuf-publisher":
    if __SPAM__:
        sys.stdout.write("x" * (65 * 1024))
        raise SystemExit(0)
    target_name = sys.argv[sys.argv.index("--target-name") + 1]
    target_sha256 = sys.argv[sys.argv.index("--target-sha256") + 1]
    if __BAD__:
        target_sha256 = "0" * 64
    retained = [
        sys.argv[index + 1]
        for index, value in enumerate(sys.argv)
        if value == "--retain-target"
    ]
    print(json.dumps({
        "retained_targets": retained,
        "target_name": target_name,
        "target_sha256": target_sha256,
        "targets_version": 19,
    }, sort_keys=True))
elif name == "channel-publisher":
    document = entry["document"]
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\\n").encode()
    print(json.dumps({
        "channel": document["channel"],
        "document_sha256": hashlib.sha256(raw).hexdigest(),
        "target_name": document["target_name"],
        "target_sha256": document["target_sha256"],
    }, sort_keys=True))
"""
    source = (
        source.replace("__LOG__", repr(str(log)))
        .replace("__SPAM__", repr(spam_tuf_receipt))
        .replace("__BAD__", repr(bad_tuf_receipt))
    )
    for name in ("oras", "tuf-publisher", "channel-publisher"):
        path = fake_bin / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
    env = os.environ | {
        "VONK_PLATFORM_ORAS_BIN": str(fake_bin / "oras"),
        "VONK_PLATFORM_TUF_PUBLISHER_BIN": str(fake_bin / "tuf-publisher"),
        "VONK_PLATFORM_CHANNEL_PUBLISHER_BIN": str(fake_bin / "channel-publisher"),
        "VONK_PLATFORM_AUTHORITY_URL": "https://authority.example.invalid",
        "VONK_PLATFORM_AUTHORITY_AUDIENCE": "vonk-forge-platform-release",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example.invalid/token",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-request-token",
        "VONK_PLATFORM_TUF_ROOT_KEY": "must-not-leak",
        "GITHUB_TOKEN": "must-not-leak",
    }
    return env, log


def _run(
    *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
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


def test_describe_bundle_emits_exact_deterministic_oci_descriptor(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    bundle = canonical_bundle
    bundle_path = tmp_path / "bundle.tar"
    bundle_path.write_bytes(bundle)

    result = _run(
        "describe-bundle",
        "--bundle",
        str(bundle_path),
        "--repository",
        "ghcr.io/example/vonk-forge/control-deployment",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == _bundle_descriptor(bundle)


def test_publish_orders_exact_bundle_target_and_discovery_channel(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    release_path, bundle_path, target_name, target_sha256 = _write_release(
        tmp_path, canonical_bundle
    )
    env, log = _fake_publishers(tmp_path)
    channel_document = {
        "channel": "stable",
        "discovery_only": True,
        "schema_version": 1,
        "target_name": target_name,
        "target_sha256": target_sha256,
        "tuf_targets_version": 19,
    }
    expected_channel_sha256 = _sha256(
        (json.dumps(channel_document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )

    result = _run(
        "publish",
        "--manifest",
        str(release_path),
        "--bundle",
        str(bundle_path),
        "--channel",
        "stable",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt == {
        "bundle": _bundle_descriptor(bundle_path.read_bytes()),
        "channel": "stable",
        "channel_document_sha256": expected_channel_sha256,
        "schema_version": 1,
        "target_name": target_name,
        "target_sha256": target_sha256,
        "tuf_targets_version": 19,
    }
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry["name"] for entry in entries] == [
        "oras",
        "oras",
        "oras",
        "tuf-publisher",
        "channel-publisher",
    ]
    assert entries[0]["argv"][:3] == [
        "blob",
        "push",
        "--media-type",
    ]
    assert entries[0]["argv"][4] == (
        "ghcr.io/example/vonk-forge/control-deployment"
        f"@sha256:{_sha256(b'{}')}"
    )
    assert LAYER_MEDIA_TYPE in entries[1]["argv"]
    assert entries[1]["argv"][4] == _bundle_descriptor(
        bundle_path.read_bytes()
    )["reference"].rsplit("@", 1)[0] + "@" + _bundle_descriptor(
        bundle_path.read_bytes()
    )["layer_digest"]
    assert entries[2]["argv"][:2] == ["manifest", "push"]
    assert entries[2]["argv"][4] == _bundle_descriptor(
        bundle_path.read_bytes()
    )["reference"]
    assert entries[3]["argv"] == [
        "publish-target",
        "--target-name",
        target_name,
        "--target-sha256",
        target_sha256,
        "--target-file",
        entries[3]["argv"][6],
    ]
    assert entries[4]["document"] == channel_document
    assert "VONK_PLATFORM_TUF_ROOT_KEY" not in entries[0]["env_keys"]
    assert "VONK_PLATFORM_AUTHORITY_URL" not in entries[0]["env_keys"]
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in entries[0]["env_keys"]
    assert "GITHUB_TOKEN" not in entries[0]["env_keys"]
    for entry in entries[3:]:
        assert "VONK_PLATFORM_TUF_ROOT_KEY" not in entry["env_keys"]
        assert "GITHUB_TOKEN" not in entry["env_keys"]
        assert "VONK_PLATFORM_AUTHORITY_URL" in entry["env_keys"]
        assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" in entry["env_keys"]


def test_split_publication_keeps_oidc_out_of_oci_upload(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    release_path, bundle_path, _, _ = _write_release(tmp_path, canonical_bundle)
    env, log = _fake_publishers(tmp_path)

    bundle = _run(
        "publish-bundle",
        "--manifest",
        str(release_path),
        "--bundle",
        str(bundle_path),
        env=env,
    )
    authority = _run(
        "publish-authority",
        "--manifest",
        str(release_path),
        "--bundle",
        str(bundle_path),
        "--channel",
        "stable",
        env=env,
    )

    assert bundle.returncode == 0, bundle.stderr
    assert authority.returncode == 0, authority.stderr
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry["name"] for entry in entries] == [
        "oras",
        "oras",
        "oras",
        "tuf-publisher",
        "channel-publisher",
    ]
    assert all(
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN" not in entry["env_keys"]
        for entry in entries[:3]
    )
    assert all(
        "DOCKER_CONFIG" not in entry["env_keys"] for entry in entries[3:]
    )


def test_target_only_publication_never_advances_the_channel(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    release_path, bundle_path, _, _ = _write_release(tmp_path, canonical_bundle)
    env, log = _fake_publishers(tmp_path)

    result = _run(
        "publish-target",
        "--manifest",
        str(release_path),
        "--bundle",
        str(bundle_path),
        env=env,
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert "tuf_targets_version" in receipt
    assert "channel" not in receipt
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry["name"] for entry in entries] == ["tuf-publisher"]


def test_channel_only_publication_consumes_the_exact_target_receipt(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    release_path, bundle_path, _, _ = _write_release(tmp_path, canonical_bundle)
    env, log = _fake_publishers(tmp_path)
    target = _run(
        "publish-target",
        "--manifest",
        str(release_path),
        "--bundle",
        str(bundle_path),
        env=env,
    )
    assert target.returncode == 0, target.stderr
    receipt = tmp_path / "target-receipt.json"
    receipt.write_text(target.stdout, encoding="utf-8")
    log.unlink()

    channel = _run(
        "publish-channel",
        "--manifest",
        str(release_path),
        "--target-receipt",
        str(receipt),
        "--channel",
        "stable",
        env=env,
    )

    assert channel.returncode == 0, channel.stderr
    published = json.loads(channel.stdout)
    assert published["channel"] == "stable"
    assert published["target_name"] == json.loads(target.stdout)["target_name"]
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry["name"] for entry in entries] == ["channel-publisher"]


def test_bundle_descriptor_mismatch_fails_before_any_publication(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    release_path, bundle_path, _, _ = _write_release(tmp_path, canonical_bundle)
    bundle_path.write_bytes(canonical_bundle[:-1] + b"X")
    env, log = _fake_publishers(tmp_path)

    result = _run(
        "publish",
        "--manifest",
        str(release_path),
        "--bundle",
        str(bundle_path),
        env=env,
    )

    assert result.returncode == 2
    assert "descriptor" in result.stderr
    assert not log.exists()


def test_wrong_tuf_receipt_does_not_publish_channel(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    release_path, bundle_path, _, _ = _write_release(tmp_path, canonical_bundle)
    env, log = _fake_publishers(tmp_path, bad_tuf_receipt=True)

    result = _run(
        "publish",
        "--manifest",
        str(release_path),
        "--bundle",
        str(bundle_path),
        env=env,
    )

    assert result.returncode == 2
    assert "TUF publication receipt" in result.stderr
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry["name"] for entry in entries] == [
        "oras",
        "oras",
        "oras",
        "tuf-publisher",
    ]


def test_oversized_tuf_output_is_bounded_and_blocks_channel(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    release_path, bundle_path, _, _ = _write_release(tmp_path, canonical_bundle)
    env, log = _fake_publishers(tmp_path, spam_tuf_receipt=True)

    result = _run(
        "publish",
        "--manifest",
        str(release_path),
        "--bundle",
        str(bundle_path),
        env=env,
    )

    assert result.returncode == 2
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry["name"] for entry in entries][-1] == "tuf-publisher"
    assert all(entry["name"] != "channel-publisher" for entry in entries)


def test_publish_rejects_latest_as_a_discovery_channel(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    release_path, bundle_path, _, _ = _write_release(tmp_path, canonical_bundle)
    env, log = _fake_publishers(tmp_path)

    result = _run(
        "publish",
        "--manifest",
        str(release_path),
        "--bundle",
        str(bundle_path),
        "--channel",
        "latest",
        env=env,
    )

    assert result.returncode == 2
    assert "channel" in result.stderr
    assert not log.exists()


def test_noncanonical_platform_manifest_fails_before_publication(
    tmp_path: Path, canonical_bundle: bytes
) -> None:
    release_path, bundle_path, _, _ = _write_release(tmp_path, canonical_bundle)
    document = json.loads(release_path.read_bytes())
    release_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    env, log = _fake_publishers(tmp_path)

    result = _run(
        "publish",
        "--manifest",
        str(release_path),
        "--bundle",
        str(bundle_path),
        env=env,
    )

    assert result.returncode == 2
    assert "canonical" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize("unsafe", ("hardlink", "world-writable"))
def test_publish_rejects_unsafe_tool_before_mutation(
    tmp_path: Path, canonical_bundle: bytes, unsafe: str
) -> None:
    release_path, bundle_path, _, _ = _write_release(tmp_path, canonical_bundle)
    env, log = _fake_publishers(tmp_path)
    tool = Path(env["VONK_PLATFORM_ORAS_BIN"])
    if unsafe == "hardlink":
        os.link(tool, tmp_path / "oras-alias")
    else:
        tool.chmod(0o777)

    result = _run(
        "publish",
        "--manifest",
        str(release_path),
        "--bundle",
        str(bundle_path),
        env=env,
    )

    assert result.returncode == 2
    assert "executable" in result.stderr
    assert not log.exists()
