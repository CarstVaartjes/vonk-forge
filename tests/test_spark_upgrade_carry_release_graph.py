"""Signed historical publications consume their immutable renderer contract."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from tests.acceptance import spark_upgrade_carry as carry
from tests.acceptance.test_spark_lifecycle import (
    COMPOSE_IMAGE_ROLES,
    LifecycleError,
    SparkLifecycle,
    _canonical,
    _write_failure_report,
)

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_SOURCE = "e5e6ea44d9bf8ec86f914c5a4d687386eb796796"
CURRENT_SOURCE = "77522666d3196e5bf8291cca7c85489ac07710aa"
GENERATION = "a" * 64


@pytest.mark.parametrize(
    "historical,ephemeral", [(True, False), (False, False), (False, True)]
)
def test_signed_source_renderer_preserves_its_complete_image_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, historical: bool, ephemeral: bool
) -> None:
    """Catches checking a baseline with retired roles against the candidate graph."""
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
    if historical:
        roles.append("ca")
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
    # Exercise the inherited Compose checks with the baseline's verified graph,
    # even though the candidate no longer publishes the retired CA role.
    lane = object.__new__(carry.UpgradeCarryLifecycle)
    lane.baseline = resolved
    lane.candidate = replace(
        resolved,
        generation="d" * 64,
        compose_image_roles={
            role: service
            for role, service in resolved.compose_image_roles.items()
            if role != "ca"
        },
    )
    lane.controller_generation = resolved.generation
    lane.controller_release = resolved.release
    lane.arguments = argparse.Namespace(
        candidate_release=resolved.release,
        baseline_release=resolved.release,
        generation=lane.candidate.generation,
        channel="dev",
    )
    lane.bundle = tmp_path
    lane.project = "carry-release-graph"
    configured_services = yaml.safe_load(resolved.overlay.read_text())["services"]
    base_services = {
        service: {
            "image": definition["image"].split("@", 1)[0].rsplit(":", 1)[0] + ":dev"
        }
        for service, definition in configured_services.items()
    }

    def compose_config(command, **_kwargs):
        services = configured_services if "-f" in command else base_services
        return SimpleNamespace(stdout=json.dumps({"services": services}))

    monkeypatch.setattr(lane, "_run_command", compose_config)
    monkeypatch.setenv(carry.OVERLAY_VARIABLE, str(resolved.overlay))
    lane._assert_compose_image_graph()
    # Shared and retired services must both retain their own release's channel.
    for role in roles:
        service = resolved.compose_image_roles[role]
        valid_image = base_services[service]["image"]
        base_services[service]["image"] = valid_image.rsplit(":", 1)[0] + ":latest"
        with pytest.raises(
            LifecycleError, match="base Compose image does not follow its channel"
        ):
            lane._assert_compose_image_graph()
        base_services[service]["image"] = valid_image
        valid_pin = configured_services[service]["image"]
        configured_services[service]["image"] = valid_pin.replace("b" * 64, "c" * 64)
        with pytest.raises(
            LifecycleError, match="Compose image graph differs from publication"
        ):
            lane._assert_compose_image_graph()
        configured_services[service]["image"] = valid_pin
    # The unoverlaid path also checks the historical role's moving alias.
    monkeypatch.delenv(carry.OVERLAY_VARIABLE)
    lane._assert_compose_image_graph()
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


@pytest.mark.parametrize(
    "channel,tag,wrong_tag", [("dev", "dev", "latest"), ("stable", "latest", "dev")]
)
def test_base_graph_uses_baseline_and_candidate_signed_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    tag: str,
    wrong_tag: str,
) -> None:
    """Catches candidate-only checks rejecting retired baseline roles or accepting unbound images."""
    monkeypatch.setenv(carry.OVERLAY_VARIABLE, str(tmp_path / "overlay.yaml"))
    candidate_images = {
        role: f"ghcr.io/carstvaartjes/vonk-forge-{role}:{tag}-sha-candidate@sha256:{'b' * 64}"
        for role in COMPOSE_IMAGE_ROLES
    }
    baseline_images = {
        "ca": f"ghcr.io/carstvaartjes/vonk-forge-ca:{tag}-sha-baseline@sha256:{'c' * 64}"
    }
    candidate_release = tmp_path / "candidate.json"
    baseline_release = tmp_path / "baseline.json"
    candidate_release.write_bytes(
        _canonical(
            {"generation": GENERATION, "channel": channel, "images": candidate_images}
        )
    )
    baseline_release.write_bytes(
        _canonical({"channel": channel, "images": baseline_images})
    )
    lane = object.__new__(SparkLifecycle)
    lane.arguments = argparse.Namespace(
        candidate_release=candidate_release,
        baseline_release=baseline_release,
        channel=channel,
        generation=GENERATION,
    )
    lane.bundle = tmp_path
    lane.project = "carry-base-channel-graph"
    configured_services = {
        service: {"image": candidate_images[role]}
        for role, service in COMPOSE_IMAGE_ROLES.items()
    }
    configured_services["hermes-litellm-key-provisioner"] = {
        "image": candidate_images["litellm"]
    }
    base_services = {
        "step-ca": {"image": f"ghcr.io/carstvaartjes/vonk-forge-ca:{tag}"},
        "control-api": {"image": f"ghcr.io/carstvaartjes/vonk-forge-api:{tag}"},
        "postgres": {"image": f"postgres:17@sha256:{'d' * 64}"},
    }

    def compose_config(command, **_kwargs):
        services = configured_services if "-f" in command else base_services
        return SimpleNamespace(stdout=json.dumps({"services": services}))

    monkeypatch.setattr(lane, "_run_command", compose_config)
    lane._assert_compose_image_graph()
    for service, invalid_image in (
        ("step-ca", f"ghcr.io/carstvaartjes/vonk-forge-unknown:{tag}"),
        ("step-ca", f"ghcr.io/carstvaartjes/vonk-forge-ca:{wrong_tag}"),
        ("control-api", f"ghcr.io/carstvaartjes/vonk-forge-api:{wrong_tag}"),
        ("postgres", "postgres:17"),
    ):
        valid_image = base_services[service]["image"]
        base_services[service]["image"] = invalid_image
        with pytest.raises(
            LifecycleError, match="base Compose image does not follow its channel"
        ):
            lane._assert_compose_image_graph()
        base_services[service]["image"] = valid_image
