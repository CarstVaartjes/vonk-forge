"""Real Docker Compose accepts every resolved profile selection as written."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml
from vonk_nas_setup.compose import resolve_compose

ROOT = Path(__file__).resolve().parents[3]
IMAGES = {
    f"{role}_image": f"ghcr.io/carstvaartjes/vonk-forge-{role}:v1.2.3"
    for role in ("api", "worker", "hermes", "litellm")
}
ENVIRONMENT = (
    "NAS_LAN_IP=192.0.2.10\nVONK_CONTROL_HOSTNAME=control.example.test\n"
    "VONK_MANAGEMENT_CIDRS=192.0.2.0/24\n"
)


def _script(name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / "scripts" / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _services(directory: Path, compose: bytes) -> dict:
    (directory / "docker-compose.yaml").write_bytes(compose)
    (directory / ".env").write_text(ENVIRONMENT)
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=directory,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return yaml.safe_load(result.stdout)["services"]


@pytest.mark.needs_docker
@pytest.mark.parametrize(
    ("selection", "profiled"),
    [
        ("", set()),
        ("secure-remote", {"tailscale-gateway", "tailscale-configurator"}),
        (
            "secure-remote,hermes",
            {
                "tailscale-gateway",
                "tailscale-configurator",
                "hermes-agent",
                "hermes-litellm-key-provisioner",
            },
        ),
        ("hermes", {"hermes-agent", "hermes-litellm-key-provisioner"}),
    ],
)
def test_compose_config_accepts_the_resolved_bundle(
    tmp_path: Path, selection: str, profiled: set[str]
) -> None:
    """Catch a resolved file that real Compose rejects or that drops/keeps services."""
    rendered = tmp_path / "rendered.yaml"
    _script("render-production-compose").render(
        ROOT / "deploy/compose/compose.yaml", rendered, **IMAGES, channel="pinned"
    )
    document = yaml.safe_load(rendered.read_text(encoding="utf-8"))
    payload = _script("build-nas-compose-bundle")._payload(document, "stable")
    resolved = resolve_compose(
        payload["docker_compose_yaml"], {"COMPOSE_PROFILES": selection}
    )
    everything = {
        name
        for name, service in document["services"].items()
        if not service.get("profiles")
    }
    services = _services(tmp_path, resolved)
    assert set(services) == everything | profiled


@pytest.mark.needs_docker
def test_compose_config_accepts_an_optional_dependency_on_an_unselected_service(
    tmp_path: Path,
) -> None:
    """Catch leaving a `required: false` entry that names an undefined service."""
    compose = (
        "services:\n  core: {image: 'busybox:1'}\n"
        "  watcher:\n    image: 'busybox:1'\n    profiles: [other]\n"
        "    depends_on:\n      core: {condition: service_started}\n"
        "      optional: {condition: service_started, required: false}\n"
        "  optional: {image: 'busybox:1', profiles: [extra]}\n"
    )
    services = _services(
        tmp_path, resolve_compose(compose, {"COMPOSE_PROFILES": "other"})
    )
    assert set(services) == {"core", "watcher"}
    assert set(services["watcher"]["depends_on"]) == {"core"}
