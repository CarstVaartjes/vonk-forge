from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_ROOT = ROOT / "deploy/compose"
DEFAULT_SERVICES = {
    "image-janitor",
    "caddy",
    "control-api",
    "control-worker",
    "litellm",
    "postgres",
    "prometheus",
    "registry",
}


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    for line in (COMPOSE_ROOT / "tests/test.env").read_text().splitlines():
        if line and not line.startswith("#"):
            name, value = line.split("=", 1)
            environment[name] = value
    return environment


def _rendered(*, secure_remote: bool = True) -> dict[str, Any]:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(COMPOSE_ROOT / "tests/test.env"),
        "-f",
        str(COMPOSE_ROOT / "compose.yaml"),
    ]
    command.extend(("config", "--format", "json"))
    environment = _environment()
    if not secure_remote:
        environment["COMPOSE_PROFILES"] = ""
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
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
    assert services["tailscale-gateway"]["profiles"] == ["secure-remote"]
    assert services["tailscale-configurator"]["profiles"] == ["secure-remote"]

    expected_evidence = {
        "image-janitor": ("unused.json", "st_mtime"),
        "postgres": ("pg_isready", "psql", "SELECT 1"),
        "control-api": ("vonk_control.healthcheck",),
        "control-worker": ("vonk_control.worker_healthcheck",),
        "litellm": ("/health/readiness",),
        "prometheus": ("/-/ready",),
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


def test_dependencies_only_order_startup_and_are_acyclic() -> None:
    """Catches a required healthy/completed edge that aborts `compose up`.

    Compose aborts dependents when a required service_healthy or
    service_completed_successfully dependency is unhealthy or exited, leaving
    them Created forever (restart policies never act on never-started
    containers). Every dependent waits or crash-restarts itself instead.

    Scope: this covers compose dependency edges only. Namespace/volume joins
    (see test_joins_on_restartable_services_are_explicitly_allowed) and
    outright dependency start failures (e.g. a port bind) can still leave a
    container Created; the image-janitor's Created-start net (PR #2166)
    starts those after CREATED_GRACE.
    """
    for selection in (True, False):
        services = _rendered(secure_remote=selection)["services"]
        assert isinstance(services, dict)
        for name, service in services.items():
            assert isinstance(service, dict)
            for dependency_name, dependency in service.get("depends_on", {}).items():
                if dependency.get("required") is False:
                    continue
                assert dependency["condition"] == "service_started", (
                    name,
                    dependency_name,
                )

    services = _rendered()["services"]
    assert isinstance(services, dict)
    graph: dict[str, set[str]] = {}
    for name, service in services.items():
        assert isinstance(service, dict)
        dependencies = service.get("depends_on", {})
        assert isinstance(dependencies, dict)
        graph[name] = set(dependencies)

    def visit(name: str, path: tuple[str, ...]) -> None:
        assert name not in path, " -> ".join(path + (name,))
        for dependency in graph[name]:
            if dependency in graph:
                visit(dependency, path + (name,))

    for service_name in graph:
        visit(service_name, ())


# A join (network namespace, container, volumes_from) is refused by the daemon
# while the target is restarting, which leaves the joiner Created; compose
# edges cannot prevent that. Each allowed joiner needs a reason.
ALLOWED_JOINS = {
    "tailscale-configurator": (
        "image-janitor starts Created containers after CREATED_GRACE, "
        "including shared-network ones (PR #2166)"
    ),
}


def test_joins_on_restartable_services_are_explicitly_allowed() -> None:
    """Catches an undocumented join on a service that restarts."""
    for selection in (True, False):
        services = _rendered(secure_remote=selection)["services"]
        for name, service in services.items():
            joins: list[str] = []
            mode = str(service.get("network_mode", ""))
            for prefix in ("service:", "container:"):
                if mode.startswith(prefix):
                    joins.append(mode.removeprefix(prefix))
            for entry in service.get("volumes_from", []):
                joins.append(str(entry).split(":")[0].removeprefix("container:"))
            for target in joins:
                restartable = (
                    target not in services
                    or services[target].get("restart", "no") != "no"
                )
                if restartable:
                    assert ALLOWED_JOINS.get(name), (
                        name,
                        target,
                    )


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


def test_default_graph_is_warning_free_and_does_not_couple_configurator_to_profile() -> (
    None
):
    """Catches render warnings and disabled-profile dependencies."""
    services = _rendered(secure_remote=False)["services"]
    assert set(services) == DEFAULT_SERVICES
    secure_remote = _rendered()["services"]
    configurator = secure_remote["tailscale-configurator"]
    assert set(configurator["depends_on"]) == {
        "caddy",
        "control-api",
        "tailscale-gateway",
    }
