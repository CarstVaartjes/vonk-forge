"""Signed historical publications consume their immutable renderer contract."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tests.acceptance import spark_upgrade_carry as carry
from tests.acceptance.test_spark_lifecycle import LifecycleError, _write_failure_report

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
    """Catches losing an image from the signed publication graph."""
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
            "install/installer-release-public.pem",
        ):
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / name).read_bytes())
        monkeypatch.setenv("VONK_ACCEPTANCE_TEST_MODE", "1")
        monkeypatch.setenv("VONK_ACCEPTANCE_RELEASE_PUBLIC_KEY", str(key))
    roles = ["api", "worker", "hermes", "litellm"]
    images = {
        role: f"ghcr.io/carstvaartjes/vonk-forge-{role}:dev-sha-{source}@sha256:{'b' * 64}"
        for role in roles
    }
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "cluster_profiles/schemas/control-openapi.json",
            b'{"paths":{},"components":{"schemas":{}}}',
        )
    wheel_content = wheel.getvalue()
    document: dict[str, object] = {
        "schema_version": 2,
        "generation": GENERATION,
        "channel": "dev",
        "source_sha": source,
        "version": "0.1.1",
        "images": images,
        "artifacts": {
            "agent-package-linux-arm64": {"package_version": "0.1.1"},
            "cli-wheel": {
                "path": f"artifacts/dev/releases/{GENERATION}/cli/test.whl",
                "size": len(wheel_content),
                "sha256": hashlib.sha256(wheel_content).hexdigest(),
            },
        },
    }
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    signature = base64.b64encode(
        signing_key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    )
    fetched: list[str] = []

    def fetch(url: str, destination: Path) -> None:
        fetched.append(url)
        if url == (
            f"https://install.example/artifacts/dev/releases/{GENERATION}/cli/test.whl"
        ):
            content = wheel_content
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
        else:
            pytest.fail(f"unbound source input {url}")
        destination.write_bytes(content)

    monkeypatch.setattr(carry, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(carry, "_fetch", fetch)
    if ephemeral:
        with monkeypatch.context() as production:
            production.delenv("VONK_ACCEPTANCE_TEST_MODE")
            with pytest.raises(LifecycleError):
                carry.resolve_release(
                    "https://localhost:9443", "dev", GENERATION, tmp_path / "production"
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
    valid_wheel = wheel_content
    wheel_content = b"tampered wheel"
    with pytest.raises(LifecycleError, match="signed digest"):
        carry.resolve_release(
            "https://install.example", "dev", GENERATION, tmp_path / "bad-wheel"
        )
    wheel_content = valid_wheel
    carry.resolve_release(
        "https://install.example", "dev", GENERATION, tmp_path / "fresh-wheel"
    )
    verified_overlay = resolved.overlay.read_bytes()
    valid_raw, valid_signature = raw, signature
    if not historical:
        images.pop("worker")
        raw = (
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        signature = base64.b64encode(
            signing_key.sign(raw, padding.PKCS1v15(), hashes.SHA256())
        )
        with pytest.raises(LifecycleError):
            carry.resolve_release(
                "https://install.example",
                "dev",
                GENERATION,
                tmp_path / "missing-worker",
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


def test_carry_startup_failure_reports_and_allows_fresh_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches incomplete lifecycle arguments masking a startup failure."""
    output = tmp_path / "report.json"
    release = SimpleNamespace(
        generation=GENERATION,
        release=tmp_path / "release.json",
        version="0.1.1",
        source_sha=CURRENT_SOURCE,
    )
    monkeypatch.setenv("VONK_ACCEPTANCE_TEST_MODE", "1")
    monkeypatch.setenv("VONK_ACCEPTANCE_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(carry, "resolve_release", lambda *args, **kwargs: release)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "spark_upgrade_carry",
            "--channel",
            "dev",
            "--candidate-generation",
            GENERATION,
            "--run-id",
            "1",
            "--output",
            str(output),
        ],
    )
    invocations = []

    @contextmanager
    def lifecycle(arguments, **_releases):
        invocations.append(arguments)
        if len(invocations) == 1:
            error = LifecycleError("candidate listener unavailable")
            _write_failure_report(arguments, error, phase="controller-startup")
            raise error
        yield SimpleNamespace(observe=lambda: {"probe_summary": {}, "observed": []})

    monkeypatch.setattr(carry, "UpgradeCarryLifecycle", lifecycle)
    assert carry.main() == 1
    report = json.loads(output.read_text())
    assert "candidate listener unavailable" in report["error"]
    assert carry.main() == 0
    assert invocations[1].source_sha == release.source_sha
    assert invocations[1].version == release.version
    assert invocations[1].output == output
