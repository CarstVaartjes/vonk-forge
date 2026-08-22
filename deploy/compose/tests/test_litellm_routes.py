from __future__ import annotations

import ast
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "deploy/compose"


def test_entrypoint_uses_supervisor_for_atomic_generated_config() -> None:
    entrypoint = COMPOSE / "litellm/entrypoint.sh"
    subprocess.run(["/bin/sh", "-n", entrypoint], check=True)
    text = entrypoint.read_text()
    assert (
        "exec python /run/vonk-normalized-secrets/runtime-assets/litellm/config_supervisor.py"
        in text
    )
    assert "litellm --config /app/config.yaml" not in text

    supervisor = COMPOSE / "litellm/config_supervisor.py"
    ast.parse(supervisor.read_text())
    source = supervisor.read_text()
    assert 'ROOT / "activation.json"' in source
    assert 'ROOT / "generations"' in source
    assert (
        '"/run/vonk-normalized-secrets/runtime-assets/litellm/bootstrap-config.json"'
        in source
    )
    assert 'Path("/supervisor")' in source
    assert "sha256" in source
    assert "MAXIMUM_LEASE" in source
    assert "terminate" in source
    assert "kill" in source
    assert "shell=True" not in source
