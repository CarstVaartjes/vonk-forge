"""Missing staged assets must end one attempt and recover on the next attempt."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest
from vonk_control.runtime_init import stage_runtime_assets

ROOT = Path(__file__).resolve().parents[3]


def _commands() -> list[tuple[str, str]]:
    loader = importlib.machinery.SourceFileLoader(
        "runtime_asset_compose", str(ROOT / "scripts/render-dev-compose")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    model = module._compose_document(ROOT / "deploy/compose/compose.yaml")
    commands = [
        (name, service["entrypoint"][2].replace("$$", "$"))
        for name, service in model["services"].items()
        if isinstance(service.get("entrypoint"), list)
        and len(service["entrypoint"]) >= 3
        and "until [ -f /run/vonk-runtime-assets/" in service["entrypoint"][2]
    ]
    assert {name for name, _command in commands} == {
        "postgres",
        "litellm",
        "prometheus",
        "grafana",
        "caddy",
        "registry",
        "tailscale-configurator",
        "hermes-litellm-key-provisioner",
    }, "a changed entrypoint must not silently remove its recovery proof"
    return commands


@pytest.mark.parametrize(("service", "command"), _commands())
def test_asset_timeout_then_staging_recovers_the_same_shell_command(
    service: str,
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wrong implementation: the old unbounded loop never returns a failure;
    # an exec before the asset exists or a lost quoted argument also fails here.
    assets = tmp_path / "assets"
    source = tmp_path / "shipped"
    source.mkdir()
    script = tmp_path / "consumer"
    script.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$EXEC_CALLS"\n')
    script.chmod(0o755)
    command = command.replace("/run/vonk-runtime-assets", str(assets))
    asset_match = re.search(r"until \[ -f ([^ ]+) \]", command)
    assert asset_match is not None
    asset = Path(asset_match[1])
    executable = command.split("; exec ", 1)[1]
    if executable.startswith("/bin/sh "):
        entrypoint = Path(executable.split()[1])
        relative = entrypoint.relative_to(assets)
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_bytes(script.read_bytes())
    else:
        # The image-owned executable is outside this asset-read boundary. Keep
        # the actual wait and arguments; substitute only that final executable.
        executable_name = executable.split()[0]
        command = command.replace(f"; exec {executable_name}", f"; exec {script}")
    relative = asset.relative_to(assets)
    if not (source / relative).exists():
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_text("shipped asset\n")
    calls = tmp_path / "exec-calls"
    sleeps = tmp_path / "sleeps"
    environment = {**os.environ, "EXEC_CALLS": str(calls), "SLEEPS": str(sleeps)}
    # Virtualize only time: the production counter/test/exit stays untouched.
    # The external subprocess timeout catches the old unbounded implementation.
    virtual_sleep = 'sleep() { printf "%s\\n" "$1" >> "$SLEEPS"; }; '
    argv = ["/bin/sh", "-c", virtual_sleep + command, service, "a b", "line\nbreak"]
    missing = subprocess.run(
        argv, env=environment, check=False, capture_output=True, text=True, timeout=2
    )
    assert missing.returncode == 1
    assert f"runtime-asset-timeout: {asset}; retry via restart policy" in missing.stderr
    assert not calls.exists(), "missing configuration must never launch a consumer"
    observations = sleeps.read_text().splitlines()
    assert 0 < len(observations) <= 120
    assert set(observations) == {"1"}

    # The real producer publishes the formerly missing files atomically.
    monkeypatch.setattr(os, "fchown", lambda *_args: None)
    stage_runtime_assets(source, assets)
    sleeps.unlink()
    repaired = subprocess.run(
        argv, env=environment, check=False, capture_output=True, text=True, timeout=2
    )
    assert repaired.returncode == 0, repaired.stderr
    assert not sleeps.exists(), "present assets must execute without another wait"
    assert calls.exists()
    if '"$@"' in command:
        assert calls.read_text() == "a b\nline\nbreak\n"
    elif service == "registry":
        assert calls.read_text() == f"{asset}\n"
    elif service == "hermes-litellm-key-provisioner":
        assert calls.read_text() == f"{asset}\n--reconcile-forever\n"
    else:
        assert calls.read_text() == "\n"
