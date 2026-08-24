from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_ROOT = ROOT / "deploy/compose"
DEFAULT_SERVICES = {
    "caddy",
    "control-api",
    "control-worker",
    "grafana",
    "litellm",
    "postgres",
    "prometheus",
    "registry",
    "step-ca",
    "tailscale-configurator",
    "tailscale-gateway",
}


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    for line in (COMPOSE_ROOT / "tests/test.env").read_text().splitlines():
        if line and not line.startswith("#"):
            name, value = line.split("=", 1)
            environment[name] = value
    return environment


def _rendered(*, hermes: bool = False) -> dict[str, object]:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(COMPOSE_ROOT / "tests/test.env"),
        "-f",
        str(COMPOSE_ROOT / "compose.yaml"),
    ]
    if hermes:
        command.extend(("--profile", "hermes"))
    command.extend(("config", "--format", "json"))
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=_environment(),
    )
    assert result.stderr == ""
    return json.loads(result.stdout)


def _health_command(service: dict[str, object]) -> str:
    healthcheck = service["healthcheck"]
    assert isinstance(healthcheck, dict)
    test = healthcheck["test"]
    assert isinstance(test, list)
    return " ".join(str(part) for part in test)


def test_every_default_service_has_a_service_specific_readiness_probe() -> None:
    """Catches healthchecks that only prove a process, socket, or marker exists."""
    services = _rendered()["services"]
    assert isinstance(services, dict)
    defaults = {
        name: service
        for name, service in services.items()
        if isinstance(service, dict) and "profiles" not in service
    }
    assert set(defaults) == DEFAULT_SERVICES

    expected_evidence = {
        "postgres": ("pg_isready", "psql", "SELECT 1"),
        "control-api": ("vonk_control.healthcheck",),
        "control-worker": ("vonk_control.worker_healthcheck",),
        "step-ca": ("step ca health",),
        "litellm": ("/health/readiness",),
        "prometheus": ("/-/ready",),
        "grafana": ("/api/health",),
        "caddy": ("127.0.0.1:8082/healthz",),
        "registry": ("127.0.0.1:5000/v2/",),
        "tailscale-gateway": ("BackendState", "Running"),
        "tailscale-configurator": ("TS_HEALTHCHECK_ONLY=1",),
    }
    process_only = ("pgrep", "pidof", "kill -0", "/proc/", "test -S", "test -f")
    for name, service in defaults.items():
        command = _health_command(service)
        assert all(token in command for token in expected_evidence[name]), name
        assert not any(token in command for token in process_only), name


def test_health_dependencies_are_acyclic_and_wait_only_for_readiness() -> None:
    """Catches service-started/completed dependencies and readiness cycles."""
    services = _rendered()["services"]
    assert isinstance(services, dict)
    graph: dict[str, set[str]] = {}
    for name, service in services.items():
        assert isinstance(service, dict)
        dependencies = service.get("depends_on", {})
        assert isinstance(dependencies, dict)
        graph[name] = set(dependencies)
        for dependency in dependencies.values():
            assert isinstance(dependency, dict)
            assert dependency["condition"] == "service_healthy"

    def visit(name: str, path: tuple[str, ...]) -> None:
        assert name not in path, " -> ".join(path + (name,))
        for dependency in graph[name]:
            if dependency in graph:
                visit(dependency, path + (name,))

    for service_name in graph:
        visit(service_name, ())


def test_tailscale_browser_forwarding_crosses_only_an_internal_edge() -> None:
    """Catches Caddy sharing the gateway's outbound control-plane network."""
    model = _rendered()
    services = model["services"]
    networks = model["networks"]
    assert isinstance(services, dict) and isinstance(networks, dict)

    assert networks["tailnet-web-edge"]["internal"] is True
    assert set(services["caddy"]["networks"]) & set(
        services["tailscale-gateway"]["networks"]
    ) == {"tailnet-web-edge"}
    assert "tailnet-control-plane" in services["tailscale-gateway"]["networks"]
    assert "tailnet-control-plane" not in services["caddy"]["networks"]


def test_default_and_hermes_graphs_are_warning_free_and_do_not_couple_configurator_to_profile() -> None:
    """Catches render warnings and disabled-profile dependencies in either graph."""
    for hermes in (False, True):
        services = _rendered(hermes=hermes)["services"]
        assert set(services) == DEFAULT_SERVICES | (
            {"hermes-agent", "hermes-litellm-key-provisioner"} if hermes else set()
        )
        configurator = services["tailscale-configurator"]
        assert set(configurator["depends_on"]) == {"caddy", "tailscale-gateway"}
