from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "deploy/compose"
IMAGE_TEMPLATE = COMPOSE / "compose.dev.images.yaml"
CONFIGURE = ROOT / "control/src/vonk_control/resources/dev/tailscale-configure.sh"
TAILSCALE_IMAGE = (
    "tailscale/tailscale:v1.98.8@sha256:"
    "d54b2e6a9c09f0e5ec52e82b9ad4af3d446b54a7c08075e92f11c39dd410105f"
)
EXPECTED_MAP = {
    "services": {
        "svc:vonk-forge": {
            "endpoints": {"tcp:443": "http://caddy:8080"}
        }
    },
    "version": "0.0.1",
}


def _rendered() -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(IMAGE_TEMPLATE),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _volumes_by_target(service: dict[str, object]) -> dict[str, dict[str, object]]:
    return {volume["target"]: volume for volume in service.get("volumes", [])}


def test_development_gateway_is_pinned_userspace_and_least_privilege() -> None:
    rendered = _rendered()
    gateway = rendered["services"]["tailscale-gateway"]

    assert gateway["image"] == TAILSCALE_IMAGE
    assert gateway["read_only"] is True
    assert gateway["cap_drop"] == ["ALL"]
    assert gateway["security_opt"] == ["no-new-privileges:true"]
    for forbidden in ("ports", "devices", "cap_add", "privileged"):
        assert not gateway.get(forbidden)
    assert gateway["environment"] == {
        "TS_AUTH_ONCE": "true",
        "TS_CLIENT_ID": "file:/run/secrets/tailscale-oauth-client-id",
        "TS_CLIENT_SECRET": "file:/run/secrets/tailscale-oauth-client-secret",
        "TS_EXTRA_ARGS": "--advertise-tags=tag:vonk-gateway",
        "TS_HOSTNAME": "vonk-forge-dev-gateway",
        "TS_SOCKET": "/var/run/tailscale/tailscaled.sock",
        "TS_STATE_DIR": "/var/lib/tailscale",
        "TS_USERSPACE": "true",
    }
    assert set(gateway["networks"]) == {"ingress", "tailnet-web-edge"}
    assert {secret["target"] for secret in gateway["secrets"]} == {
        "/run/secrets/tailscale-oauth-client-id",
        "/run/secrets/tailscale-oauth-client-secret",
    }
    volumes = _volumes_by_target(gateway)
    assert volumes["/var/lib/tailscale"]["source"] == "dev-tailscale-state"
    assert volumes["/var/run/tailscale"]["source"] == "dev-tailscale-socket"
    assert all(
        volume["target"] != "/var/run/docker.sock" for volume in volumes.values()
    )


def test_development_configurator_has_only_bounded_runtime_authority() -> None:
    rendered = _rendered()
    configurator = rendered["services"]["tailscale-configurator"]

    assert configurator["image"] == TAILSCALE_IMAGE
    assert configurator["network_mode"] == "service:tailscale-gateway"
    assert configurator["read_only"] is True
    assert configurator["cap_drop"] == ["ALL"]
    assert configurator["security_opt"] == ["no-new-privileges:true"]
    for forbidden in (
        "ports",
        "networks",
        "devices",
        "cap_add",
        "privileged",
        "secrets",
    ):
        assert not configurator.get(forbidden)
    volumes = _volumes_by_target(configurator)
    assert set(volumes) == {
        "/var/run/tailscale",
        "/run/vonk-runtime",
        "/run/vonk-tailnet",
    }
    assert volumes["/var/run/tailscale"]["source"] == "dev-tailscale-socket"
    assert volumes["/run/vonk-runtime"]["read_only"] is True
    assert volumes["/run/vonk-tailnet"]["source"] == "dev-tailscale-runtime"
    assert configurator["depends_on"] == {
        "caddy": {
            "condition": "service_healthy",
            "required": True,
        },
        "tailscale-gateway": {
            "condition": "service_healthy",
            "required": True,
        }
    }


def test_configurator_health_requires_active_https_capability_and_exact_map() -> None:
    configurator = _rendered()["services"]["tailscale-configurator"]
    health_command = " ".join(configurator["healthcheck"]["test"])

    assert "serve status --json" in health_command
    assert "status --json" in health_command
    assert '"HTTPS":true' in health_command
    assert "service-host" in health_command
    assert "serve get-config --all" in health_command


def test_hostname_handoff_orders_caddy_without_a_startup_cycle() -> None:
    rendered = _rendered()
    services = rendered["services"]
    caddy = services["caddy"]
    configurator = services["tailscale-configurator"]

    assert caddy["environment"]["VONK_CONTROL_HOSTNAME_FILE"] == (
        "/run/vonk-tailnet/control-hostname"
    )
    assert "VONK_CONTROL_HOSTNAME" not in caddy["environment"]
    assert set(caddy["networks"]) == {
        "application",
        "ingress",
        "tailnet-web-edge",
    }
    caddy_volumes = _volumes_by_target(caddy)
    assert caddy_volumes["/run/vonk-tailnet"] == {
        "type": "volume",
        "source": "dev-tailscale-runtime",
        "target": "/run/vonk-tailnet",
        "read_only": True,
        "volume": {},
    }
    assert "tailscale-configurator" not in caddy["depends_on"]
    assert configurator["depends_on"]["caddy"] == {
        "condition": "service_healthy",
        "required": True,
    }
    assert not services["tailscale-gateway"].get("depends_on")
    assert "8080" not in {
        port["published"]
        for service in services.values()
        for port in service.get("ports", [])
        if port.get("host_ip") not in {"127.0.0.1", "::1"}
    }


def _fake_cli(
    tmp_path: Path,
    *,
    expected: str,
    suffix: str,
    drifted: bool,
) -> tuple[Path, Path]:
    calls = tmp_path / "calls.log"
    repaired = tmp_path / "repaired"
    fake = tmp_path / "tailscale"
    initial_config = (
        '{"version":"0.0.1","services":{"svc:vonk-forge":'
        '{"endpoints":{"tcp:443":"http://wrong:80"}},"svc:extra":'
        '{"endpoints":{"tcp:80":"http://plaintext:80"}}}}'
        if drifted
        else expected
    )
    fake.write_text(
        "#!/bin/sh\n"
        f"calls={calls}\n"
        f"repaired={repaired}\n"
        "printf '%s\\n' \"$*\" >>\"$calls\"\n"
        "case \"$*\" in\n"
        "  *\"serve get-config --all\"*)\n"
        f"    if [ -f \"$repaired\" ]; then printf '%s\\n' '{expected}'; "
        f"else printf '%s\\n' '{initial_config}'; fi ;;\n"
        "  *\"serve status --json\"*)\n"
        "    if [ -f \"$repaired\" ]; then printf '%s\\n' '{\"Services\":{\"svc:vonk-forge\":{\"TCP\":{\"443\":{\"HTTPS\":true}}}}}'; "
        "else printf '%s\\n' '{\"Services\":{\"svc:vonk-forge\":{\"TCP\":{\"443\":{\"HTTP\":true}}}}}'; fi ;;\n"
        "  *\"--service=svc:vonk-forge --https=443 http://caddy:8080\"*) touch \"$repaired\" ;;\n"
        f"  *\"status --json\"*) printf '%s\\n' '{{\"CurrentTailnet\":{{\"MagicDNSSuffix\":\"{suffix}\"}},\"Self\":{{\"CapMap\":{{\"service-host\":[]}}}}}}' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake, calls


def _run_reconciler(
    tmp_path: Path,
    *,
    suffix: str,
    drifted: bool,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    socket_path = tmp_path / "tailscaled.sock"
    daemon_socket = socket.socket(socket.AF_UNIX)
    daemon_socket.bind(str(socket_path))
    output = tmp_path / "tailnet-runtime"
    output.mkdir()
    target = output / "control-hostname"
    target.write_text("vonk-forge.stale-tailnet.ts.net\n", encoding="utf-8")
    expected = json.dumps(EXPECTED_MAP, sort_keys=True, separators=(",", ":"))
    _, calls = _fake_cli(
        tmp_path,
        expected=expected,
        suffix=suffix,
        drifted=drifted,
    )
    try:
        result = subprocess.run(
            ["/bin/sh", CONFIGURE],
            env=os.environ
            | {
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "TS_CONFIGURE_ONCE": "1",
                "TS_SOCKET_PATH": str(socket_path),
                "TS_HOSTNAME_OUTPUT": str(target),
                "TS_CLIENT_ID": "secret-client-id-sentinel",
                "TS_CLIENT_SECRET": "secret-client-secret-sentinel",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    finally:
        daemon_socket.close()
    return result, target, calls


def test_reconciler_repairs_drift_and_publishes_exact_live_service_hostname(
    tmp_path: Path,
) -> None:
    result, target, calls = _run_reconciler(
        tmp_path,
        suffix="team-example.ts.net",
        drifted=True,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_text() == "vonk-forge.team-example.ts.net\n"
    assert "https://vonk-forge.team-example.ts.net/" in result.stdout
    assert "secret-client-id-sentinel" not in result.stdout + result.stderr
    assert "secret-client-secret-sentinel" not in result.stdout + result.stderr
    commands = calls.read_text()
    for command in (
        "serve reset",
        "--service=svc:vonk-forge --https=443 http://caddy:8080",
        "serve advertise svc:vonk-forge",
    ):
        assert command in commands
    for forbidden in ("svc:extra", "svc:hermes", "funnel", "--http=", "--tcp="):
        assert forbidden not in commands


def test_reconciler_rejects_invalid_tailnet_suffix_without_stale_publication(
    tmp_path: Path,
) -> None:
    result, target, _calls = _run_reconciler(
        tmp_path,
        suffix="malicious.invalid",
        drifted=False,
    )

    assert result.returncode != 0
    assert not target.exists()
    assert "malicious.invalid" not in result.stdout + result.stderr


def test_development_reconciler_source_has_no_public_or_secret_output_path() -> None:
    subprocess.run(["/bin/sh", "-n", CONFIGURE], check=True)
    source = CONFIGURE.read_text(encoding="utf-8").lower()

    for forbidden in (
        "funnel",
        "ts_client_id",
        "ts_client_secret",
        "oauth",
        "docker.sock",
        "svc:*",
        "svc:hermes",
    ):
        assert forbidden not in source
