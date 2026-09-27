from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "deploy/compose"


def test_compose_keeps_tailscale_opt_in_and_publishes_local_ca_https() -> None:
    compose = yaml.safe_load((COMPOSE / "compose.yaml").read_text())
    tailscale = yaml.safe_load((COMPOSE / "tailscale/compose.yaml").read_text())
    caddyfile = (COMPOSE / "Caddyfile").read_text()

    assert tailscale["services"]["tailscale-gateway"]["profiles"] == ["secure-remote"]
    assert tailscale["services"]["tailscale-configurator"]["profiles"] == [
        "secure-remote"
    ]
    assert compose["services"]["caddy"]["ports"][0]["host_ip"] == (
        "${NAS_LAN_IP:?set reserved NAS LAN IP}"
    )
    assert (
        "tls /run/secrets/controller-server-certificate /run/secrets/controller-server-key"
        in caddyfile
    )
    assert "reverse_proxy 127.0.0.1:8080" in caddyfile


def test_litellm_starts_with_an_empty_optional_upstream_key(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python = fake_bin / "python"
    python.write_text(
        "#!/bin/sh\nprintf '%s:%s' \"${LITELLM_UPSTREAM_KEY+x}\" "
        '"${LITELLM_UPSTREAM_KEY:-}" > "$CAPTURE"\n'
    )
    python.chmod(0o755)
    master = tmp_path / "master"
    database = tmp_path / "database"
    master.write_text("sk-master\n")
    database.write_text("postgresql://litellm:test@postgres/litellm\n")
    capture = tmp_path / "captured"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "LITELLM_MASTER_KEY_FILE": str(master),
        "LITELLM_DATABASE_URL_FILE": str(database),
        "LITELLM_UPSTREAM_KEY_FILE": str(tmp_path / "not-provided"),
        "CAPTURE": str(capture),
    }

    result = subprocess.run(
        ["sh", str(COMPOSE / "litellm/entrypoint.sh")],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert capture.read_text() == "x:"
