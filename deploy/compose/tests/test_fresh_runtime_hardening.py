from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_ROOT = ROOT / "deploy/compose"
CONTROL_DOCKERFILE = ROOT / "control/Dockerfile"


def _document(name: str) -> dict[str, Any]:
    content = yaml.safe_load((COMPOSE_ROOT / name).read_text(encoding="utf-8"))
    assert isinstance(content, dict)
    return content


def _final_stage(name: str) -> str:
    dockerfile = CONTROL_DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(
        rf"(?ms)^FROM [^\n]+ AS {re.escape(name)}\n(?P<body>.*?)(?=^FROM |\Z)",
        dockerfile,
    )
    assert match is not None
    return match.group("body")


def test_fresh_postgres_initializes_the_litellm_database() -> None:
    canonical = _document("compose.yaml")
    postgres = canonical["services"]["postgres"]

    assert "litellm-database-password" in postgres["secrets"]
    # The Controller image ships the entrypoint; PostgreSQL runs the staged copy.
    assert postgres["entrypoint"] == [
        "/bin/sh",
        "/run/vonk-runtime-assets/postgres/entrypoint.sh",
    ]
    assert postgres["command"] == ["postgres"]
    assert "runtime-assets:/run/vonk-runtime-assets:ro" in postgres["volumes"]
    api_root = _final_stage("api-root")
    assert "COPY deploy/compose/postgres/ " in api_root
    assert canonical["secrets"]["litellm-database-password"] == {
        "file": "./secrets/litellm-database-password"
    }


def test_prometheus_does_not_pass_a_value_to_its_boolean_lifecycle_flag() -> None:
    command = _document("compose.yaml")["services"]["prometheus"]["command"]

    assert not any(
        argument.startswith("--web.enable-lifecycle") for argument in command
    )


def test_read_only_litellm_places_prisma_cache_on_tmpfs() -> None:
    litellm = _document("compose.yaml")["services"]["litellm"]

    assert litellm["read_only"] is True
    assert "/tmp" in litellm["tmpfs"]
    assert "/root:exec,mode=0700,uid=10002,gid=10001" in litellm["tmpfs"]
    assert litellm["environment"]["HOME"] == "/root"
    assert litellm["environment"]["XDG_CACHE_HOME"] == "/root/.cache"


def test_caddy_healthcheck_does_not_depend_on_a_virtual_host() -> None:
    canonical = _document("compose.yaml")
    healthcheck = canonical["services"]["caddy"]["healthcheck"]["test"]
    caddyfile = (COMPOSE_ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert healthcheck == [
        "CMD-SHELL",
        (
            "wget -q -O /dev/null http://127.0.0.1:8082/healthz"
            " && ip route | grep -q '^default'"
        ),
    ]
    assert "http://127.0.0.1:8082" in caddyfile


def test_caddy_retries_control_api_startup_for_a_bounded_interval() -> None:
    caddyfile = (COMPOSE_ROOT / "Caddyfile").read_text(encoding="utf-8")

    # The API can remain unavailable while PostgreSQL DNS and migrations settle
    # after a host restart. Keep the edge retry finite: callers get a response,
    # while agents avoid a burst of transient 502s during normal recovery.
    assert caddyfile.count("lb_try_duration 3s") == 3
    assert caddyfile.count("lb_try_interval 250ms") == 3


def test_caddy_serves_the_site_controller_certificate() -> None:
    canonical = _document("compose.yaml")
    caddy = canonical["services"]["caddy"]
    caddyfile = (COMPOSE_ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert set(caddy["secrets"]) >= {
        "controller-server-certificate",
        "controller-server-key",
    }
    assert (
        caddyfile.count(
            "tls {$VONK_CONTROLLER_TLS_CERTIFICATE:/run/secrets/controller-server-certificate} "
            "{$VONK_CONTROLLER_TLS_KEY:/run/secrets/controller-server-key}"
        )
        == 3
    )


@pytest.mark.parametrize("renewed", [True, False])
def test_caddy_serves_the_renewed_leaf_or_falls_back_to_the_installer_leaf(
    tmp_path: Path, renewed: bool
) -> None:
    """Catches a Caddy that cannot start on a Compose file without the volume.

    Pull and redeploy without an installer rerun keeps the previous Compose
    file: no controller-tls volume. Caddy then serves the installer's leaf.
    """
    run = tmp_path / "run"
    for name in (
        "secrets/controller-server-certificate",
        "secrets/controller-server-key",
        "secrets/agent-client-ca",
    ):
        (run / name).parent.mkdir(parents=True, exist_ok=True)
        (run / name).write_text("installer\n")
    (run / "secrets/agent-proxy-auth").write_text("a" * 32 + "\n")
    if renewed:
        for name in ("controller-server-certificate", "controller-server-key"):
            (run / "vonk-controller-tls").mkdir(exist_ok=True)
            (run / "vonk-controller-tls" / name).write_text("renewed\n")
    script = tmp_path / "entrypoint.sh"
    script.write_text(
        (COMPOSE_ROOT / "caddy/entrypoint.sh")
        .read_text(encoding="utf-8")
        .replace("/run/", f"{run}/")
        .replace("/config/", f"{tmp_path}/config/")
    )

    served = subprocess.run(
        [
            "sh",
            str(script),
            "sh",
            "-c",
            'cat "$VONK_CONTROLLER_TLS_CERTIFICATE" "$VONK_CONTROLLER_TLS_KEY"',
        ],
        env={"PATH": os.environ["PATH"], "VONK_CONTROL_HOSTNAME": "vonk-forge.local"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert served == ("renewed\nrenewed\n" if renewed else "installer\ninstaller\n")


def test_control_images_do_not_install_git_or_ssh() -> None:
    worker = _final_stage("worker")
    api = _final_stage("api")
    for stage in (worker, api):
        assert "apt-get install" not in stage
        assert "openssh" not in stage
        assert " git" not in stage


def test_api_image_runs_non_root_and_seals_source_secret_directory() -> None:
    worker = _final_stage("worker")
    api = _final_stage("api")

    assert "USER 10001:10001" in worker
    assert "api_preexec" not in worker
    assert "CMD" not in worker
    assert "USER 10001:10001" in api
    assert 'ENTRYPOINT ["python", "-m", "vonk_control.api_preexec"]' in api
    assert 'CMD ["python", "-m", "vonk_control.api"]' in api
    assert "install -d -o 0 -g 0 -m 0700 /run/secrets" in _final_stage("api-root")


def test_every_staged_runtime_asset_a_service_reads_ships_in_the_controller_image() -> (
    None
):
    """Catches a config a pulled release cannot deliver (it would drift)."""
    prefix = "/usr/local/share/vonk-forge/runtime-assets/"
    shipped: set[str] = set()
    for line in _final_stage("api-root").splitlines():
        if not line.startswith("COPY deploy/compose/") or prefix not in line:
            continue
        *sources, destination = line.split()[1:]
        target = destination.removeprefix(prefix)
        for source in sources:
            path = ROOT / source
            files = sorted(p for p in path.rglob("*") if p.is_file())
            if path.is_file():
                files = [path]
            for file in files:
                inner = (
                    file.relative_to(path).as_posix() if path.is_dir() else file.name
                )
                shipped.add(target + inner)
    readers = [
        COMPOSE_ROOT / "compose.yaml",
        COMPOSE_ROOT / "tailscale/compose.yaml",
        COMPOSE_ROOT / "caddy/entrypoint.sh",
        COMPOSE_ROOT / "postgres/entrypoint.sh",
        COMPOSE_ROOT / "litellm/entrypoint.sh",
        COMPOSE_ROOT / "litellm/config_supervisor.py",
        COMPOSE_ROOT / "prometheus/prometheus.yml",
    ]
    referenced = {
        match
        for reader in readers
        for match in re.findall(
            r"/run/vonk-runtime-assets/([A-Za-z0-9_./-]+)",
            reader.read_text(encoding="utf-8"),
        )
    }
    assert {"caddy/Caddyfile", "postgres/entrypoint.sh"} <= shipped
    assert referenced
    for reference in referenced:
        path = reference.rstrip("/")
        assert path in shipped or any(
            item.startswith(path + "/") for item in shipped
        ), path
