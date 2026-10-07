"""Signed historical publications consume their immutable renderer contract."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tests.acceptance import spark_upgrade_carry as carry
from tests.acceptance.test_spark_lifecycle import LifecycleError

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_SOURCE = "f8a65ee9eeb6ab82e3e31004fcc159140f5b2b44"
CURRENT_SOURCE = "77522666d3196e5bf8291cca7c85489ac07710aa"
GENERATION = "a" * 64


@pytest.mark.parametrize("historical", [True, False])
def test_signed_source_renderer_preserves_its_complete_image_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, historical: bool
) -> None:
    """Catches applying today's fifth role to a signed four-role publication."""
    source = HISTORICAL_SOURCE if historical else CURRENT_SOURCE
    renderer = (
        ROOT / f"tests/fixtures/accepted-release-renderers/{source}.py"
        if historical
        else ROOT / "scripts/render-accepted-compose-overlay"
    )
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = tmp_path / "install/installer-release-public.pem"
    key.parent.mkdir()
    key.write_bytes(
        signing_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    roles = ["api", "worker", "hermes", "litellm"]
    if not historical:
        roles.append("ca")
    images = {
        role: f"ghcr.io/carstvaartjes/vonk-forge-{role}:dev-sha-{source}@sha256:{'b' * 64}"
        for role in roles
    }
    document: dict[str, object] = {
        "schema_version": 2,
        "generation": GENERATION,
        "channel": "dev",
        "source_sha": source,
        "version": "0.1.1",
        "images": images,
        "artifacts": {"agent-package-linux-arm64": {"package_version": "0.1.1"}},
    }
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    signature = base64.b64encode(
        signing_key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    )
    fetched: list[str] = []

    def fetch(url: str, destination: Path) -> None:
        fetched.append(url)
        if url.endswith("/release.json"):
            content = raw
        elif url.endswith("/release.sig"):
            content = signature
        elif (
            url
            == f"https://raw.githubusercontent.com/CarstVaartjes/vonk-forge/{source}/scripts/render-accepted-compose-overlay"
        ):
            content = renderer.read_bytes()
        elif url.endswith(f"/{source}/deploy/compose/Caddyfile"):
            content = b"example.test {}\n"
        elif url.endswith(f"/{source}/control/openapi.json"):
            content = b'{"paths":{},"components":{"schemas":{}}}'
        else:
            pytest.fail(f"unbound source input {url}")
        destination.write_bytes(content)

    monkeypatch.setattr(carry, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(carry, "_fetch", fetch)
    resolved = carry.resolve_release(
        "https://install.example", "dev", GENERATION, tmp_path
    )
    assert set(resolved.compose_image_roles) == set(roles)
    assert ("step-ca" in resolved.overlay.read_text()) is not historical
    assert all(source in url for url in fetched if "raw.githubusercontent.com" in url)
    if not historical:
        images.pop("ca")
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        signature = base64.b64encode(
            signing_key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
        )
        with pytest.raises(LifecycleError, match="image graph is invalid"):
            carry.resolve_release(
                "https://install.example", "dev", GENERATION, tmp_path / "missing-ca"
            )
    # Changing signed source identity must fail before fetching executable code.
    fetched.clear()
    document["source_sha"] = "c" * 40
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with pytest.raises(LifecycleError, match="signature is invalid"):
        carry.resolve_release(
            "https://install.example", "dev", GENERATION, tmp_path / "tampered"
        )
    assert not any("raw.githubusercontent.com" in url for url in fetched)
