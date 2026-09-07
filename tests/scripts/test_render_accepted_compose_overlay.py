from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/render-accepted-compose-overlay"


def test_overlay_contains_only_exact_accepted_images(tmp_path: Path) -> None:
    digest = "a" * 64
    release = tmp_path / "release.json"
    release.write_text(
        json.dumps(
            {
                "channel": "dev",
                "generation": "c" * 64,
                "schema_version": 2,
                "images": {
                    role: (
                        f"ghcr.io/carstvaartjes/vonk-forge-{role}:"
                        f"dev-sha-x@sha256:{digest}"
                    )
                    for role in ("api", "worker", "hermes", "litellm")
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    private_key = tmp_path / "key.pem"
    public_key = tmp_path / "public.pem"
    subprocess.run(
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "RSA",
            "-pkeyopt",
            "rsa_keygen_bits:2048",
            "-out",
            str(private_key),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        capture_output=True,
    )
    signature = tmp_path / "release.sig"
    subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(private_key),
            "-out",
            str(signature),
            str(release),
        ],
        check=True,
        capture_output=True,
    )
    signature.write_bytes(
        __import__("base64").b64encode(signature.read_bytes()) + b"\n"
    )
    output = tmp_path / "overlay.yml"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--release",
            str(release),
            "--channel",
            "dev",
            "--generation",
            "c" * 64,
            "--signature",
            str(signature),
            "--public-key",
            str(public_key),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    rendered = output.read_text()
    assert "control-api:" in rendered
    assert "hermes-litellm-key-provisioner:" in rendered
    assert rendered.count("vonk-forge-litellm:dev-sha-x@sha256:") == 2
    assert (
        "image: ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-x@sha256:" + digest
        in rendered
    )
    assert "build:" not in rendered


def test_overlay_rejects_noncanonical_release(tmp_path: Path) -> None:
    release = tmp_path / "release.json"
    release.write_text(
        '{"channel":"dev","generation":"'
        + "c" * 64
        + '","images":{},"schema_version":2}\n'
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--release",
            str(release),
            "--channel",
            "dev",
            "--generation",
            "c" * 64,
            "--signature",
            str(tmp_path / "bad.sig"),
            "--public-key",
            str(tmp_path / "bad.pem"),
            "--output",
            str(tmp_path / "overlay.yml"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "image graph" in result.stderr


def test_overlay_rejects_bad_signature_and_wrong_repository(tmp_path: Path) -> None:
    digest = "a" * 64
    release = tmp_path / "release.json"
    release.write_text(
        json.dumps(
            {
                "channel": "dev",
                "generation": "c" * 64,
                "images": {
                    role: (
                        f"ghcr.io/carstvaartjes/vonk-forge-{role}:dev-sha-x@sha256:{digest}"
                    )
                    for role in ("api", "worker", "hermes", "litellm")
                },
                "schema_version": 2,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    signature = tmp_path / "bad.sig"
    signature.write_text("bm90IGEgc2lnbmF0dXJl\n")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--release",
            str(release),
            "--channel",
            "dev",
            "--generation",
            "c" * 64,
            "--signature",
            str(signature),
            "--public-key",
            str(tmp_path / "missing.pem"),
            "--output",
            str(tmp_path / "overlay.yml"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "signature" in result.stderr
