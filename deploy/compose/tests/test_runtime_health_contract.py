from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_ROOT = ROOT / "deploy/compose"
DEFAULT_SERVICES = {
    "caddy",
    "control-api",
    "control-signer",
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
        "control-signer": ("vonk_control.signer_healthcheck",),
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


def test_default_and_hermes_graphs_do_not_couple_configurator_startup_to_profile() -> None:
    """Catches disabled-profile dependency warnings in the default graph."""
    for hermes in (False, True):
        configurator = _rendered(hermes=hermes)["services"]["tailscale-configurator"]
        assert set(configurator["depends_on"]) == {"caddy", "tailscale-gateway"}


def _acceptance_failure_or_skip(message: str) -> None:
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


def _compose_rows(raw: str) -> list[dict[str, object]]:
    content = raw.strip()
    if not content:
        return []
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return [json.loads(line) for line in content.splitlines()]
    if isinstance(value, list):
        return value
    assert isinstance(value, dict)
    return [value]


def _assert_default_project_healthy(command: list[str], environment: dict[str, str]) -> None:
    result = subprocess.run(
        [*command, "ps", "--all", "--format", "json"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    rows = _compose_rows(result.stdout)
    assert {row["Service"] for row in rows} == DEFAULT_SERVICES
    for row in rows:
        assert row["State"] == "running", row
        assert row["Health"] == "healthy", row
        assert row.get("ExitCode") in {None, 0}, row


def test_published_bundle_converges_from_empty_state_and_after_restart(
    tmp_path: Path,
) -> None:
    """Run the real generated default graph when published acceptance inputs exist."""
    docker = shutil.which("docker")
    if docker is None:
        _acceptance_failure_or_skip("Docker CLI is required for runtime acceptance")
    info = subprocess.run(
        [docker, "info"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if info.returncode != 0:
        _acceptance_failure_or_skip("Docker daemon is required for runtime acceptance")

    source_value = os.environ.get("VONK_RUNTIME_ACCEPTANCE_BUNDLE", "")
    if not source_value:
        _acceptance_failure_or_skip(
            "VONK_RUNTIME_ACCEPTANCE_BUNDLE must select a generated published-image bundle"
        )
    source = Path(source_value)
    if (
        not source.is_dir()
        or source.is_symlink()
        or {path.name for path in source.iterdir()} != {
            "docker-compose.yaml",
            ".env",
            "secrets",
        }
        or not (source / "secrets").is_dir()
    ):
        pytest.fail("runtime acceptance bundle violates the generated three-item contract")

    bundle = tmp_path / "vonk-forge"
    shutil.copytree(source, bundle, symlinks=True)
    assert not any(path.is_symlink() for path in bundle.rglob("*"))
    project = "vonk-health-" + uuid.uuid4().hex
    environment = os.environ | {"COMPOSE_PROJECT_NAME": project}
    command = [
        docker,
        "compose",
        "--env-file",
        str(bundle / ".env"),
        "-f",
        str(bundle / "docker-compose.yaml"),
    ]
    subprocess.run(
        [*command, "config", "--quiet"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    try:
        subprocess.run(
            [*command, "down", "--volumes", "--remove-orphans"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=120,
        )
        first = subprocess.run(
            [*command, "up", "-d", "--wait"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=600,
        )
        assert "warning" not in first.stderr.lower()
        _assert_default_project_healthy(command, environment)

        subprocess.run(
            [*command, "restart"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=300,
        )
        restarted = subprocess.run(
            [*command, "up", "-d", "--wait"],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=600,
        )
        assert "warning" not in restarted.stderr.lower()
        _assert_default_project_healthy(command, environment)
    finally:
        subprocess.run(
            [*command, "down", "--volumes", "--remove-orphans"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=180,
        )
