from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/acceptance/test_fresh_nas_install.py"


def _acceptance_module():
    spec = importlib.util.spec_from_file_location("fresh_nas_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_hermes_bundle_receives_the_expensive_reference_rollout(
    tmp_path: Path,
) -> None:
    acceptance = _acceptance_module()
    default = tmp_path / "default"
    hermes = tmp_path / "hermes"

    assert acceptance.reference_rollout_bundles(default, hermes) == (hermes,)


def test_authenticated_service_checks_construct_secret_backed_requests(
    tmp_path: Path, monkeypatch
) -> None:
    acceptance = _acceptance_module()
    fixture = tmp_path / "compose"
    fixture.write_text("#!/bin/sh\n")
    fixture.chmod(0o755)
    calls = []
    monkeypatch.setenv("VONK_ACCEPTANCE_REFERENCE_COMPOSE", str(fixture))
    monkeypatch.setattr(acceptance, "run", lambda command, **_: calls.append(command) or SimpleNamespace(stdout=""))

    acceptance.verify_authenticated_service_routes(tmp_path)

    assert [command[3] for command in calls] == ["litellm", "prometheus", "grafana", "registry"]
    assert "Authorization: Bearer $(cat /run/vonk-normalized-secrets/prometheus-metrics-token)" in calls[1][-1]
    assert "grafana-admin-password" in calls[2][-1]
    assert "http://127.0.0.1:5000/v2/" in calls[3][-1]
