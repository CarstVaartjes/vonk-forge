from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cluster_profiles.runtime_identity import contract_fingerprint

ROOT = Path(__file__).resolve().parents[2]


SCRIPT = ROOT / "scripts/install-release-publication"


DIGEST = "a" * 64


SOURCE_SHA = "b" * 40


SPARK_PLATFORMS = ("linux-arm64",)


ACCEPTANCE_GATES = {
    "compose_compatibility_lower",
    "compose_compatibility_ugreen",
    "compose_default",
    "nas_site_secret_preservation",
    "nas_workstation",
    "spark_arm64",
    "spark_job",
    "spark_pairing",
    "spark_renewal",
}


def _canonical(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")


def _input_mapping(inputs: dict[str, object], key: str) -> dict[str, object]:
    value = inputs[key]
    if not isinstance(value, dict):
        raise TypeError(f"release input {key!r} is not a platform mapping")
    return value


def _agent_package(tmp_path: Path, platform: str, version: str) -> Path:
    architecture = platform.removeprefix("linux-")
    root = tmp_path / f"package-root-{version}-{architecture}"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "DEBIAN/control").write_text(
        "Package: vonk-forge-agent\n"
        f"Version: {version}\n"
        f"Architecture: {architecture}\n"
        "Maintainer: test <test@example.test>\n"
        "Description: test\n",
        encoding="utf-8",
    )
    binary = ("agent-" + version).encode()
    helper = ("helper-" + version).encode()
    for name, raw in (("vonk-agent", binary), ("vonk-agent-helper", helper)):
        destination = root / "usr/lib/vonk-forge" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    key = Ed25519PrivateKey.from_private_bytes(b"k" * 32)
    public = root / "usr/share/keyrings/vonk-forge-release.pub"
    public.parent.mkdir(parents=True)
    public.write_text(key.public_key().public_bytes_raw().hex() + "\n")
    package = tmp_path / f"vonk-forge-agent_{version}_{architecture}.deb"
    subprocess.run(
        ["/usr/bin/dpkg-deb", "--build", "--root-owner-group", root, package],
        check=True,
        capture_output=True,
    )
    Path(f"{package}.host.sig").write_text(
        key.sign(
            b"VONK-HOST-ARTIFACT-V1\x00deb\x00"
            + hashlib.sha256(package.read_bytes()).digest()
        ).hex()
        + "\n"
    )
    _canonical(
        package.with_suffix(".provenance.json"),
        {
            "predicate": {
                "buildDefinition": {
                    "externalParameters": {"build_digest": "sha256:" + "c" * 64}
                }
            },
            "subject": [
                {"digest": {"sha256": hashlib.sha256(raw).hexdigest()}, "name": name}
                for name, raw in (("vonk-agent", binary), ("vonk-agent-helper", helper))
            ],
        },
    )
    return package


def _inputs(
    tmp_path: Path,
    *,
    version: str = "1.2.3",
    channel: str = "stable",
) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    signing_key = tmp_path / "installer-signing-key.pem"
    signing_public_key = tmp_path / "installer-signing-public.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            signing_key,
        ],
        check=True,
        capture_output=True,
    )
    os.chmod(signing_key, 0o600)
    subprocess.run(
        ["openssl", "pkey", "-in", signing_key, "-pubout", "-out", signing_public_key],
        check=True,
        capture_output=True,
    )
    nas_wheel = tmp_path / "vonk_nas_setup-0.1.1-py3-none-any.whl"
    with zipfile.ZipFile(nas_wheel, "w") as archive:
        root = "vonk_nas_setup-0.1.1.dist-info"
        archive.writestr(root + "/METADATA", "Name: vonk-nas-setup\nVersion: 0.1.1\n")
        archive.writestr(root + "/WHEEL", "Root-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(
            root + "/entry_points.txt",
            "[console_scripts]\nvonk-nas-setup = vonk_nas_setup.cli:main\n",
        )
        for name in (
            "vonk_nas_setup/cli.py",
            "vonk_agent_protocol/__init__.py",
            "vonk_agent_protocol/installer_setup.py",
        ):
            archive.writestr(name, "# Portable setup fixture\n")
    spark: dict[str, Path] = {}
    packages: dict[str, Path] = {}
    baseline_version = "0.0.0~acceptance.1+g" + SOURCE_SHA[:12]
    baseline_packages: dict[str, Path] = {}
    for platform in SPARK_PLATFORMS:
        path = tmp_path / f"vonk-spark-setup-{platform}"
        path.write_bytes(f"spark setup {platform}\n".encode())
        spark[platform] = path
        packages[platform] = _agent_package(tmp_path, platform, version)
        baseline_packages[platform] = _agent_package(
            tmp_path, platform, baseline_version
        )
    payload = tmp_path / "payload.json"
    _canonical(
        payload,
        {
            "docker_compose_yaml": "services: {}\n",
            "schema_version": 2,
        },
    )
    cli_wheel = tmp_path / "vonk_cluster_profiles-0.1.1-py3-none-any.whl"
    with zipfile.ZipFile(cli_wheel, "w") as archive:
        archive.writestr(
            "cluster_profiles/build-identity.json",
            json.dumps(
                {
                    "schema_version": 2,
                    "control_contract_sha256": contract_fingerprint(
                        json.loads(
                            (
                                ROOT
                                / "src/cluster_profiles/schemas/control-openapi.json"
                            ).read_text()
                        )
                    ),
                    "worker_contract_sha256": contract_fingerprint(
                        json.loads(
                            (
                                ROOT
                                / "rust/crates/vonk-agent-protocol/schema/wire.json"
                            ).read_text()
                        )
                    ),
                    "source_sha": SOURCE_SHA,
                    "release_version": version,
                }
            ),
        )
        archive.writestr(
            "cluster_profiles/schemas/control-openapi.json",
            (ROOT / "src/cluster_profiles/schemas/control-openapi.json").read_bytes(),
        )
    return {
        "nas_wheel": nas_wheel,
        "spark": spark,
        "packages": packages,
        "baseline_packages": baseline_packages,
        "baseline_version": baseline_version,
        "payload": payload,
        "cli_wheel": cli_wheel,
        "signing_key": signing_key,
        "signing_public_key": signing_public_key,
        "version": version,
        "channel": channel,
    }


def _assemble_command(tmp_path: Path, inputs: dict[str, object]) -> list[str]:
    version = str(inputs["version"])
    channel = str(inputs["channel"])
    image_tag = f"v{version}" if channel == "stable" else f"dev-sha-{SOURCE_SHA}"
    command = [
        sys.executable,
        str(SCRIPT),
        "assemble",
        "--channel",
        channel,
        "--version",
        version,
        "--source-sha",
        SOURCE_SHA,
        "--images-source-sha",
        SOURCE_SHA,
        "--origin",
        "https://install.vonkforge.ai",
        "--expires-at",
        str(int(time.time()) + 7 * 24 * 60 * 60),
        "--signing-key",
        str(inputs["signing_key"]),
        "--signing-public-key",
        str(inputs["signing_public_key"]),
        "--api-image",
        f"ghcr.io/carstvaartjes/vonk-forge-api:{image_tag}@sha256:{DIGEST}",
        "--worker-image",
        f"ghcr.io/carstvaartjes/vonk-forge-worker:{image_tag}@sha256:{DIGEST}",
        "--litellm-image",
        f"ghcr.io/carstvaartjes/vonk-forge-litellm:{image_tag}@sha256:{DIGEST}",
        "--nas-payload",
        str(inputs["payload"]),
        "--cli-wheel",
        str(inputs["cli_wheel"]),
        "--output",
        str(tmp_path / "publication"),
    ]
    command.extend(("--nas-setup-wheel", str(inputs["nas_wheel"])))
    for platform, path in _input_mapping(inputs, "spark").items():
        command.extend(("--spark-setup", f"{platform}={path}"))
    for platform, path in _input_mapping(inputs, "packages").items():
        command.extend(("--agent-package", f"{platform}={path}"))
    for platform, path in _input_mapping(inputs, "baseline_packages").items():
        command.extend(("--agent-baseline-package", f"{platform}={path}"))
    return command


def _assemble(tmp_path: Path, inputs: dict[str, object]) -> Path:
    result = subprocess.run(
        _assemble_command(tmp_path, inputs),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return tmp_path / "publication"


@pytest.fixture(scope="module")
def assembled_publication(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run the real producer once; consumers do not need new signing identities."""
    root = tmp_path_factory.mktemp("assembled-publication")
    return _assemble(root, _inputs(root))


@pytest.fixture
def publication(tmp_path: Path, assembled_publication: Path) -> Path:
    """Keep tampering and deleted objects isolated while reusing verified bytes."""
    destination = tmp_path / "inputs/publication"
    shutil.copytree(assembled_publication, destination)
    return destination


def _acceptance_receipt(
    tmp_path: Path,
    publication: Path,
    *,
    generation: str | None = None,
    status: str = "accepted",
) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    plan = json.loads((publication / "publication-plan.json").read_text())
    private_key = tmp_path / "acceptance-private.pem"
    public_key = tmp_path / "acceptance-public.pem"
    receipt = tmp_path / "acceptance.json"
    signature = tmp_path / "acceptance.sig"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            private_key,
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["openssl", "pkey", "-in", private_key, "-pubout", "-out", public_key],
        check=True,
        capture_output=True,
    )
    _canonical(
        receipt,
        {
            "channel": plan["channel"],
            "gates": {name: True for name in sorted(ACCEPTANCE_GATES)},
            "generation": generation or plan["generation"],
            "issued_at": int(time.time()),
            "run_id": 123456,
            "schema_version": 2,
            "source_sha": SOURCE_SHA,
            "status": status,
            "version": plan["version"],
        },
    )
    raw_signature = tmp_path / "acceptance.raw.sig"
    subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            private_key,
            "-out",
            raw_signature,
            receipt,
        ],
        check=True,
        capture_output=True,
    )
    signature.write_bytes(base64.b64encode(raw_signature.read_bytes()) + b"\n")
    return receipt, signature, public_key


def _publish_candidate(
    publication: Path, destination: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "publish-candidate",
            "--bundle",
            str(publication),
            "--filesystem",
            str(destination),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _promote(
    publication: Path,
    destination: Path,
    receipt: Path,
    signature: Path,
    public_key: Path,
    *,
    preflight_only: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _promote_command(
            publication,
            receipt,
            signature,
            public_key,
            filesystem=destination,
            preflight_only=preflight_only,
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _promote_command(
    publication: Path,
    receipt: Path,
    signature: Path,
    public_key: Path,
    *,
    filesystem: Path | None = None,
    remote: str | None = None,
    preflight_only: bool = False,
) -> list[str]:
    if (filesystem is None) == (remote is None):
        raise AssertionError("select one promotion destination")
    encoded = subprocess.run(
        ["openssl", "pkey", "-pubin", "-in", public_key, "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    destination = (
        ["--filesystem", str(filesystem)]
        if filesystem is not None
        else ["--rclone-remote", str(remote)]
    )
    return [
        sys.executable,
        str(SCRIPT),
        "promote",
        "--bundle",
        str(publication),
        "--accepted-evidence",
        str(receipt),
        "--accepted-evidence-signature",
        str(signature),
        "--acceptance-public-key",
        str(public_key),
        "--acceptance-public-key-sha256",
        hashlib.sha256(encoded).hexdigest(),
        *(["--preflight-only"] if preflight_only else []),
        *destination,
    ]
