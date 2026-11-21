"""Caddy never writes the proxy-auth secret into its persistent volumes."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from vonk_control.runtime_init import (
    stage_runtime_assets,
    write_runtime_asset_inventory,
)

from deploy.compose.tests.test_runtime_asset_startup import _services

ROOT = Path(__file__).resolve().parents[3]


def _docker(*argv: str, timeout: float = 60) -> str:
    result = subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=timeout, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _pulled(name: str) -> dict:
    """The production service; pulling its image is setup, not the test."""
    service = _services()[name]
    if shutil.which("docker") is not None:
        subprocess.run(
            ["docker", "pull", "-q", service["image"]],
            capture_output=True,
            timeout=300,
            check=False,
        )
    return service


@pytest.fixture(scope="module")
def caddy() -> dict:
    return _pulled("caddy")


@pytest.fixture
def containers() -> Iterator[list[str]]:
    names: list[str] = []
    yield names
    for name in names:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, timeout=30, check=False
        )


def _caddy_kit(root: Path, caddyfile: str) -> Path:
    """One release's Caddy kit: the shipped entrypoint and that release's
    Caddyfile."""
    kit = root / "caddy"
    kit.mkdir(parents=True)
    shutil.copy(ROOT / "deploy/compose/caddy/entrypoint.sh", kit)
    (kit / "Caddyfile").write_text(caddyfile)
    assert write_runtime_asset_inventory(root)
    return root


def _docker_logs(container: str) -> str:
    result = subprocess.run(
        ["docker", "logs", container],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.stdout + result.stderr


def _caddy_secrets(secrets: Path, hostname: str, proxy_auth: str) -> None:
    """Production-shaped secret files: one self-signed certificate for every
    SNI name the entrypoint derives (so Caddy manages none itself) and the
    proxy-auth token."""
    key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(UTC)
    names = [
        hostname,
        *(f"{prefix}.{hostname}" for prefix in ("enroll", "agents")),
    ]
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in names]),
            critical=False,
        )
        .sign(key, algorithm=None)
    ).public_bytes(serialization.Encoding.PEM)
    secrets.mkdir()
    (secrets / "controller-server-certificate").write_bytes(certificate)
    (secrets / "agent-client-ca").write_bytes(certificate)
    (secrets / "controller-server-key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (secrets / "agent-proxy-auth").write_text(proxy_auth + "\n")


def _wget(container: str, url: str) -> str | None:
    result = subprocess.run(
        ["docker", "exec", container, "wget", "-qO-", url],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _persisted_secret_files(container: str, secret: str) -> list[str]:
    """Files in Caddy's writable state that hold the secret."""
    found = _docker(
        "exec",
        container,
        "sh",
        "-c",
        'grep -rlF -- "$1" /config /data || true',
        "-",
        secret,
    )
    return found.split()


@pytest.mark.needs_docker
@pytest.mark.slow(60)
def test_caddy_never_persists_the_proxy_auth_secret(
    caddy: dict,
    containers: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wrong implementation: Caddy autosaved its adapted config, with the
    # proxy-auth header value, to /config/caddy/autosave.json on every start,
    # in the caddy-config volume.
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    secret = "vonk-test-sentinel-" + uuid.uuid4().hex
    secrets = tmp_path / "secrets"
    _caddy_secrets(secrets, "control.test.example", secret)
    shipped = (ROOT / "deploy/compose/Caddyfile").read_text()
    # The control: the shipped Caddyfile minus the fix.
    unfixed = shipped.replace("\tpersist_config off\n", "")
    assert unfixed != shipped

    volumes: list[str] = []
    try:
        started = {}
        for name, caddyfile in (("fixed", shipped), ("unfixed", unfixed)):
            assets = tmp_path / name / "assets"
            stage_runtime_assets(_caddy_kit(tmp_path / name / "kit", caddyfile), assets)
            for path in (assets, *assets.rglob("*")):
                path.chmod(0o755 if path.is_dir() else 0o444)
            volume = f"vonk-caddy-config-{name}-{uuid.uuid4().hex[:8]}"
            volumes.append(_docker("volume", "create", volume).strip())
            # An earlier release's autosave, in the same named volume.
            _docker(
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{volume}:/config",
                "--entrypoint",
                "/bin/sh",
                caddy["image"],
                "-c",
                'mkdir -p /config/caddy && printf "%s" "$1" > /config/caddy/autosave.json',
                "-",
                secret,
            )
            container = f"vonk-caddy-persist-{name}-{uuid.uuid4().hex[:8]}"
            containers.append(container)
            _docker(
                "run",
                "-d",
                "--name",
                container,
                "--network",
                "none",
                "--read-only",
                "--tmpfs",
                "/tmp",
                "--tmpfs",
                "/data",
                "--cap-drop",
                "ALL",
                *(f"--cap-add={capability}" for capability in caddy["cap_add"]),
                *(f"--security-opt={option}" for option in caddy["security_opt"]),
                "-e",
                "VONK_CONTROL_HOSTNAME=control.test.example",
                "-v",
                f"{assets}:/run/vonk-runtime-assets:ro",
                "-v",
                f"{secrets}:/run/secrets:ro",
                "-v",
                f"{volume}:/config",
                "--entrypoint",
                caddy["entrypoint"][0],
                caddy["image"],
                *caddy["entrypoint"][1:],
            )
            started[name] = container

        fixed = started["fixed"]
        unfixed_container = started["unfixed"]
        deadline = time.monotonic() + 20
        health = "http://127.0.0.1:8082/healthz"
        while _wget(fixed, health) is None or _wget(unfixed_container, health) is None:
            assert time.monotonic() < deadline, _docker_logs(fixed)
            time.sleep(0.5)
        assert _persisted_secret_files(unfixed_container, secret) == [
            "/config/caddy/autosave.json"
        ], "the failure mode reproduces"
        assert _persisted_secret_files(fixed, secret) == []
    finally:
        for container in containers:
            subprocess.run(
                ["docker", "rm", "-f", container],
                capture_output=True,
                timeout=30,
                check=False,
            )
        for volume in volumes:
            subprocess.run(
                ["docker", "volume", "rm", volume],
                capture_output=True,
                timeout=30,
                check=False,
            )
