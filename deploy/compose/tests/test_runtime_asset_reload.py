"""A redeploy restages runtime assets after their consumers have started.

Compose restarts every consumer once control-api has *started*
(``depends_on: restart: true``); the API pre-exec stages the pulled release's
assets only afterwards. A consumer that reads its assets once at start keeps
the previous release's copy. That is how ``NasComposeBundleStale`` shipped in
``alerts.yaml`` and never fired in production.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from vonk_control.runtime_init import (
    stage_runtime_assets,
    write_runtime_asset_inventory,
)

ROOT = Path(__file__).resolve().parents[3]

# Consumers that load their assets only when they start, so after a redeploy
# they run the previous release's copy until their next restart. Each entry
# is a known instance of the class above; a new consumer must reload or be
# named here with its reason.
RESTART_ONLY = {
    "postgres": "Compose does not restart it with the Controller (no control-api "
    "dependency: the database must not bounce with it); init scripts run only on "
    "an empty data directory, and entrypoint and backup-loop changes apply from "
    "its next start",
    "litellm": "the supervisor rereads bootstrap-config.json every poll; "
    "entrypoint.sh and config_supervisor.py are its own process code",
    "registry": "distribution has no configuration reload",
    "tailscale-configurator": "configure.sh is its own reconcile loop; the "
    "healthcheck already runs the restaged copy",
    "hermes-litellm-key-provisioner": "provision-litellm-key.py is its own "
    "reconcile loop; the --check healthcheck already runs the restaged copy",
}


def _services() -> dict[str, dict]:
    loader = importlib.machinery.SourceFileLoader(
        "runtime_asset_reload_compose", str(ROOT / "scripts/render-dev-compose")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module._compose_document(ROOT / "deploy/compose/compose.yaml")["services"]


def test_every_runtime_asset_consumer_reloads_or_is_named() -> None:
    consumers = {
        name
        for name, service in _services().items()
        if "runtime-assets:/run/vonk-runtime-assets:ro" in service.get("volumes", [])
    }
    # These reload; the tests below prove each with its real image.
    assert consumers - set(RESTART_ONLY) == {"prometheus", "caddy"}
    assert set(RESTART_ONLY) <= consumers, "drop entries for retired consumers"


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
def prometheus() -> dict:
    return _pulled("prometheus")


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


def _kit(root: Path, alerts: str | None) -> Path:
    """One release's public kit: the shipped Prometheus files, optionally
    with that release's own rules."""
    kit = root / "prometheus"
    shutil.copytree(ROOT / "deploy/compose/prometheus", kit)
    if alerts is not None:
        (kit / "alerts.yaml").write_text(alerts)
    assert write_runtime_asset_inventory(root)
    return root


def _rules(container: str) -> set[str] | None:
    result = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "wget",
            "-qO-",
            "http://127.0.0.1:9090/api/v1/rules",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        return None
    groups = json.loads(result.stdout)["data"]["groups"]
    return {rule["name"] for group in groups for rule in group["rules"]}


def _shipped_rules() -> set[str]:
    groups = json.loads((ROOT / "deploy/compose/prometheus/alerts.yaml").read_text())
    return {rule["alert"] for group in groups["groups"] for rule in group["rules"]}


@pytest.mark.needs_docker
@pytest.mark.slow(60)
def test_prometheus_started_on_old_assets_applies_the_restaged_rules(
    prometheus: dict,
    containers: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wrong implementation: production's Prometheus, started by Compose before
    # the pre-exec restaged, evaluated the previous release's rules for good.
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    assets = tmp_path / "assets"
    previous = (
        '{"groups":[{"name":"previous","rules":'
        '[{"alert":"PreviousRelease","expr":"vector(1)"}]}]}\n'
    )
    stage_runtime_assets(_kit(tmp_path / "previous", previous), assets)
    for path in (assets, *assets.rglob("*")):
        path.chmod(0o755 if path.is_dir() else 0o444)

    script = prometheus["entrypoint"][2].replace("$$", "$")
    command = [str(argument) for argument in prometheus["command"]]
    # The control: the production command minus reloading, as before the fix.
    without_reload = [a for a in command if not a.startswith("--config.auto-reload")]
    for name, arguments in (("reload", command), ("start-only", without_reload)):
        container = f"vonk-asset-reload-{name}-{uuid.uuid4().hex[:8]}"
        containers.append(container)
        _docker(
            "run",
            "-d",
            "--name",
            container,
            "--network",
            "none",
            "--user",
            str(prometheus["user"]),
            "-v",
            f"{assets}:/run/vonk-runtime-assets:ro",
            "--entrypoint",
            prometheus["entrypoint"][0],
            prometheus["image"],
            prometheus["entrypoint"][1],
            script,
            *prometheus["entrypoint"][3:],
            *arguments,
        )
    reloading, start_only = containers

    deadline = time.monotonic() + 20
    while not _rules(reloading) == _rules(start_only) == {"PreviousRelease"}:
        assert time.monotonic() < deadline, _docker("logs", reloading)
        time.sleep(0.5)

    # The pulled release's pre-exec restages its kit into the shared volume.
    stage_runtime_assets(_kit(tmp_path / "pulled", None), assets)

    shipped = _shipped_rules()
    deadline = time.monotonic() + 35
    while _rules(reloading) != shipped:
        assert time.monotonic() < deadline, "restaged rules never loaded:\n" + (
            _docker("logs", reloading)
        )
        time.sleep(1)
    assert _rules(start_only) == {"PreviousRelease"}, "the failure mode reproduces"


def _caddy_kit(root: Path, caddyfile: str) -> Path:
    """One release's Caddy kit: the shipped entrypoint and that release's
    Caddyfile."""
    kit = root / "caddy"
    kit.mkdir(parents=True)
    shutil.copy(ROOT / "deploy/compose/caddy/entrypoint.sh", kit)
    (kit / "Caddyfile").write_text(caddyfile)
    assert write_runtime_asset_inventory(root)
    return root


def _caddyfile(route: str) -> str:
    return (
        "{\n\tadmin off\n\tauto_https off\n}\n"
        f':8080 {{\n\trespond /release "{route}"\n}}\n'
    )


def _docker_logs(container: str) -> str:
    result = subprocess.run(
        ["docker", "logs", container],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.stdout + result.stderr


def _served(container: str) -> str | None:
    result = subprocess.run(
        ["docker", "exec", container, "wget", "-qO-", "http://127.0.0.1:8080/release"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


@pytest.mark.needs_docker
@pytest.mark.slow(60)
def test_caddy_started_on_old_assets_applies_the_restaged_caddyfile(
    caddy: dict,
    containers: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wrong implementation: production's Caddy, started by Compose before the
    # pre-exec restaged, served the previous release's routes for good.
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    assets = tmp_path / "assets"
    stage_runtime_assets(
        _caddy_kit(tmp_path / "previous", _caddyfile("previous")), assets
    )
    for path in (assets, *assets.rglob("*")):
        path.chmod(0o755 if path.is_dir() else 0o444)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in (
        "controller-server-certificate",
        "controller-server-key",
        "agent-client-ca",
    ):
        (secrets / name).write_text("fixture\n")
    (secrets / "agent-proxy-auth").write_text("a" * 32)

    # The control: the native default command minus watching, as before the
    # fix. The fixed container runs the native default itself.
    native = (ROOT / "deploy/compose/caddy/entrypoint.sh").read_text()
    default = next(
        line.split()[2:] for line in native.splitlines() if "set -- caddy run" in line
    )
    for name, arguments in (
        ("watch", []),
        ("start-only", [a for a in default if a != "--watch"]),
    ):
        container = f"vonk-asset-reload-caddy-{name}-{uuid.uuid4().hex[:8]}"
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
            "--tmpfs",
            "/config",
            "--cap-drop",
            "ALL",
            *(f"--cap-add={capability}" for capability in caddy["cap_add"]),
            *(f"--security-opt={option}" for option in caddy["security_opt"]),
            "-e",
            "VONK_CONTROL_HOSTNAME=example.invalid",
            "-v",
            f"{assets}:/run/vonk-runtime-assets:ro",
            "-v",
            f"{secrets}:/run/secrets:ro",
            "--entrypoint",
            caddy["entrypoint"][0],
            caddy["image"],
            caddy["entrypoint"][1],
            caddy["entrypoint"][2].replace("$$", "$"),
            *caddy["entrypoint"][3:],
            *arguments,
        )
    watching, start_only = containers

    deadline = time.monotonic() + 20
    while not _served(watching) == _served(start_only) == "previous":
        assert time.monotonic() < deadline, _docker_logs(watching)
        time.sleep(0.5)

    # The pulled release's pre-exec restages its kit into the shared volume.
    # Its route reads a variable the entrypoint exports, as production's do.
    pulled = "pulled {$VONK_AGENT_HOSTNAME}"
    stage_runtime_assets(_caddy_kit(tmp_path / "pulled", _caddyfile(pulled)), assets)

    deadline = time.monotonic() + 20
    while _served(watching) != "pulled agents.example.invalid":
        assert time.monotonic() < deadline, "restaged Caddyfile never applied:\n" + (
            _docker_logs(watching)
        )
        time.sleep(0.5)
    assert _served(start_only) == "previous", "the failure mode reproduces"

    # A release whose Caddyfile cannot load is logged, never silent, and the
    # running routes keep serving.
    stage_runtime_assets(_caddy_kit(tmp_path / "broken", ":8080 {\n"), assets)
    deadline = time.monotonic() + 20
    while "unable to load latest config" not in _docker_logs(watching):
        assert time.monotonic() < deadline, _docker_logs(watching)
        time.sleep(0.5)
    assert _served(watching) == "pulled agents.example.invalid"
