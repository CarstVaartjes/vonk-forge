from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/collect-platform-artifact-evidence"


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_collector_binds_exact_manifest_and_canonical_attestations(
    tmp_path: Path,
) -> None:
    manifest = b'{"schemaVersion":2}'
    digest = hashlib.sha256(manifest).hexdigest()
    manifest_path = tmp_path / "manifest.json"
    sbom_path = tmp_path / "sbom.json"
    provenance_path = tmp_path / "provenance.json"
    output = tmp_path / "evidence.json"
    manifest_path.write_bytes(manifest)
    sbom_path.write_bytes(_canonical({"kind": "sbom"}))
    provenance_path.write_bytes(_canonical({"kind": "provenance"}))

    result = subprocess.run(
        [
            SCRIPT,
            "--locator",
            "control.images.api",
            "--name",
            "api",
            "--reference",
            f"ghcr.io/example/api@sha256:{digest}",
            "--manifest",
            manifest_path,
            "--sbom",
            sbom_path,
            "--provenance",
            provenance_path,
            "--output",
            output,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(output.read_bytes())
    assert evidence == {
        "artifact": {
            "name": "api",
            "provenance_sha256": hashlib.sha256(provenance_path.read_bytes()).hexdigest(),
            "reference": f"ghcr.io/example/api@sha256:{digest}",
            "sbom_sha256": hashlib.sha256(sbom_path.read_bytes()).hexdigest(),
            "sha256": digest,
            "size": len(manifest),
        },
        "locator": "control.images.api",
        "schema_version": 1,
    }
    assert output.read_bytes() == _canonical(evidence)


def test_collector_rejects_digest_mismatch_and_noncanonical_attestation(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "manifest.json"
    sbom = tmp_path / "sbom.json"
    provenance = tmp_path / "provenance.json"
    manifest.write_bytes(b"{}")
    sbom.write_text('{"kind": "sbom"}\n')
    provenance.write_bytes(_canonical({"kind": "provenance"}))

    result = subprocess.run(
        [
            SCRIPT,
            "--locator",
            "control.images.api",
            "--name",
            "api",
            "--reference",
            f"ghcr.io/example/api@sha256:{'0' * 64}",
            "--manifest",
            manifest,
            "--sbom",
            sbom,
            "--provenance",
            provenance,
            "--output",
            tmp_path / "evidence.json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert not (tmp_path / "evidence.json").exists()
