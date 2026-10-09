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


@pytest.mark.parametrize(
    "historical,ephemeral", [(True, False), (False, False), (False, True)]
)
def test_signed_source_renderer_preserves_its_complete_image_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, historical: bool, ephemeral: bool
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
    if ephemeral:
        key = tmp_path / "test-authority/ephemeral.pem"
    key.parent.mkdir()
    key.write_bytes(
        signing_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    if ephemeral:
        for name in (
            "scripts/render-accepted-compose-overlay",
            "deploy/compose/Caddyfile",
            "control/openapi.json",
            "install/installer-release-public.pem",
        ):
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / name).read_bytes())
        monkeypatch.setenv("VONK_ACCEPTANCE_TEST_MODE", "1")
        monkeypatch.setenv("VONK_ACCEPTANCE_RELEASE_PUBLIC_KEY", str(key))
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

    def fetch(url: str, destination: Path, *, decoder=None):
        fetched.append(url)
        if url.startswith("file://"):
            content = (tmp_path / "control/openapi.json").read_bytes()
        elif url.endswith("/release.json"):
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
        return decoder(content) if decoder is not None else None

    monkeypatch.setattr(carry, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(carry, "_fetch", fetch)
    if ephemeral:
        with monkeypatch.context() as production:
            production.delenv("VONK_ACCEPTANCE_TEST_MODE")
            with pytest.raises(LifecycleError):
                carry.resolve_release(
                    "https://localhost:8443", "dev", GENERATION, tmp_path / "production"
                )
        # A refused candidate leaves no busy state; the explicit test request works.
    resolved = carry.resolve_release(
        "https://install.example",
        "dev",
        GENERATION,
        tmp_path,
        acceptance_baseline=ephemeral,
    )
    if ephemeral:
        assert resolved.package_version == document["version"]
        assert not any("raw.githubusercontent.com" in url for url in fetched)
    assert set(resolved.compose_image_roles) == set(roles)
    assert all(source in url for url in fetched if "raw.githubusercontent.com" in url)
    verified_overlay = resolved.overlay.read_bytes()
    valid_raw, valid_signature = raw, signature
    if not historical:
        images.pop("ca")
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        signature = base64.b64encode(
            signing_key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
        )
        with pytest.raises(LifecycleError):
            carry.resolve_release(
                "https://install.example", "dev", GENERATION, tmp_path / "missing-ca"
            )
    # Changing signed source identity must fail before fetching executable code.
    fetched.clear()
    document["source_sha"] = "c" * 40
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with pytest.raises(LifecycleError):
        carry.resolve_release(
            "https://install.example", "dev", GENERATION, tmp_path / "tampered"
        )
    assert not any("raw.githubusercontent.com" in url for url in fetched)
    assert resolved.overlay.read_bytes() == verified_overlay
    raw, signature = valid_raw, valid_signature
    recovered = carry.resolve_release(
        "https://install.example", "dev", GENERATION, tmp_path / "recovered"
    )
    assert recovered.overlay.read_bytes() == verified_overlay


@pytest.mark.parametrize("exhausts", [False, True])
def test_unreadable_peer_contract_is_reobserved_without_poisoning_fresh_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exhausts: bool,
) -> None:
    """Catches treating an unreadable peer reply as a permanent refusal."""
    import io

    from tests.acceptance.controller_contract import ControllerContract

    valid = b'{"paths":{},"components":{"schemas":{}}}'
    target = tmp_path / "contract.json"
    target.write_bytes(valid)
    attempts = []
    repaired = False

    def reply(request, *, timeout):
        attempts.append(request)
        return io.BytesIO(
            valid
            if repaired or (not exhausts and len(attempts) > 1)
            else b"unreadable peer reply"
        )

    monkeypatch.setattr(carry.urllib.request, "urlopen", reply)
    monkeypatch.setattr(carry, "FETCH_OBSERVATION_SECONDS", 0.05)
    decoder = lambda raw: ControllerContract(
        json.loads(raw), label="observed Controller"
    )
    if exhausts:
        with pytest.raises(LifecycleError):
            carry._fetch("https://peer.example/contract", target, decoder=decoder)
        assert target.read_bytes() == valid
        repaired = True
    observed = carry._fetch("https://peer.example/contract", target, decoder=decoder)
    assert isinstance(observed, ControllerContract)
    assert len(attempts) >= 2
    assert target.read_bytes() == valid
