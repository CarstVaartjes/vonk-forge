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
    "postgres": "entrypoint and init scripts run once per start",
    "litellm": "the config supervisor and bootstrap config load once per start",
    "caddy": "Caddyfile loads once per start; caddy --watch is development-only",
    "registry": "the registry has no configuration reload",
    "tailscale-configurator": "the reconciler script loads once per start",
    "hermes-litellm-key-provisioner": "the reconciler script loads once per start",
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
    # Prometheus reloads; the test below proves it with the real image.
    assert consumers - set(RESTART_ONLY) == {"prometheus"}
    assert set(RESTART_ONLY) <= consumers, "drop entries for retired consumers"


def _docker(*argv: str, timeout: float = 60) -> str:
    result = subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=timeout, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def prometheus() -> dict:
    """The production service; pulling its image is setup, not the test."""
    service = _services()["prometheus"]
    if shutil.which("docker") is not None:
        subprocess.run(
            ["docker", "pull", "-q", service["image"]],
            capture_output=True,
            timeout=300,
            check=False,
        )
    return service


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
