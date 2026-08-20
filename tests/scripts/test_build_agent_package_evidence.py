from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build-agent-package-evidence"


def _write(path: Path, content: bytes) -> None:
    path.write_bytes(content)


def test_evidence_binds_exact_package_and_attestation_digests(tmp_path: Path) -> None:
    package = tmp_path / "vonk-forge-agent_1.2.0_arm64.deb"
    sbom = tmp_path / "vonk-forge-agent_1.2.0_arm64.sbom.spdx.json"
    provenance = tmp_path / "vonk-forge-agent_1.2.0_arm64.provenance.json"
    sigstore = tmp_path / "vonk-forge-agent_1.2.0_arm64.deb.sigstore.json"
    output = tmp_path / "evidence.json"
    _write(package, b"package")
    _write(sbom, b"sbom")
    _write(provenance, b"provenance")
    _write(sigstore, b"sigstore")

    result = subprocess.run(
        [
            SCRIPT,
            "--package",
            package,
            "--sbom",
            sbom,
            "--provenance",
            provenance,
            "--sigstore-bundle",
            sigstore,
            "--version",
            "1.2.0",
            "--architecture",
            "linux-arm64",
            "--output",
            output,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(output.read_bytes())
    assert evidence["locator"] == "agent_packages.linux-arm64"
    assert evidence["package"] == {
        "architecture": "linux-arm64",
        "filename": package.name,
        "name": "vonk-forge-agent",
        "provenance_sha256": hashlib.sha256(b"provenance").hexdigest(),
        "sbom_sha256": hashlib.sha256(b"sbom").hexdigest(),
        "sha256": hashlib.sha256(b"package").hexdigest(),
        "sigstore_bundle_sha256": hashlib.sha256(b"sigstore").hexdigest(),
        "size": len(b"package"),
        "version": "1.2.0",
    }


def test_evidence_rejects_filename_version_mismatch(tmp_path: Path) -> None:
    package = tmp_path / "vonk-forge-agent_1.1.0_arm64.deb"
    for suffix in ("sbom.spdx.json", "provenance.json", "deb.sigstore.json"):
        _write(tmp_path / f"vonk-forge-agent_1.2.0_arm64.{suffix}", b"x")
    _write(package, b"package")
    result = subprocess.run(
        [
            SCRIPT,
            "--package",
            package,
            "--sbom",
            tmp_path / "vonk-forge-agent_1.2.0_arm64.sbom.spdx.json",
            "--provenance",
            tmp_path / "vonk-forge-agent_1.2.0_arm64.provenance.json",
            "--sigstore-bundle",
            tmp_path / "vonk-forge-agent_1.2.0_arm64.deb.sigstore.json",
            "--version",
            "1.2.0",
            "--architecture",
            "linux-arm64",
            "--output",
            tmp_path / "evidence.json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "package filename" in result.stderr


def test_evidence_uses_linux_amd64_for_the_amd64_package(tmp_path: Path) -> None:
    prefix = tmp_path / "vonk-forge-agent_1.2.0_amd64"
    package = tmp_path / f"{prefix.name}.deb"
    sbom = tmp_path / f"{prefix.name}.sbom.spdx.json"
    provenance = tmp_path / f"{prefix.name}.provenance.json"
    sigstore = tmp_path / f"{prefix.name}.deb.sigstore.json"
    output = tmp_path / "evidence.json"
    for path in (package, sbom, provenance, sigstore):
        _write(path, path.name.encode())

    result = subprocess.run(
        [
            SCRIPT,
            "--package",
            package,
            "--sbom",
            sbom,
            "--provenance",
            provenance,
            "--sigstore-bundle",
            sigstore,
            "--version",
            "1.2.0",
            "--architecture",
            "linux-amd64",
            "--output",
            output,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(output.read_bytes())
    assert evidence["locator"] == "agent_packages.linux-amd64"
    assert evidence["package"]["architecture"] == "linux-amd64"
