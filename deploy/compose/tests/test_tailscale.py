from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "deploy/compose"
TAILSCALE_IMAGE = (
    "tailscale/tailscale:v1.98.8@sha256:"
    "d54b2e6a9c09f0e5ec52e82b9ad4af3d446b54a7c08075e92f11c39dd410105f"
)


def test_default_tailscale_image_matches_the_audited_lock() -> None:
    lock = json.loads((COMPOSE / "images.lock.json").read_text())
    source = (COMPOSE / "tailscale/compose.yaml").read_text()

    assert lock["images"]["tailscale"] == TAILSCALE_IMAGE
    assert source.count(TAILSCALE_IMAGE) == 2


EXPECTED_MAP = {
    "version": "0.0.1",
    "services": {
        "svc:vonk-forge": {"endpoints": {"tcp:443": "http://caddy:8080"}},
        "svc:hermes-api": {
            "endpoints": {"tcp:443": "http://hermes-agent:8642"}
        },
        "svc:hermes-dashboard": {
            "endpoints": {"tcp:443": "http://hermes-agent:9119"}
        },
    },
}


def test_production_gateway_retains_exactly_three_service_endpoints() -> None:
    script = (COMPOSE / "tailscale/configure.sh").read_text(encoding="utf-8")

    assert json.dumps(EXPECTED_MAP, sort_keys=True, separators=(",", ":")) in script
    assert script.count("--service=svc:") == 3
    assert script.count("serve advertise svc:") == 3


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    for line in (COMPOSE / "tests/test.env").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            environment[key] = value
    return environment


def _rendered() -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE / "compose.yaml"),
            "-f",
            str(COMPOSE / "compose.step-ca.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_environment(),
    )
    return json.loads(result.stdout)


def _volume_targets(service: dict[str, object]) -> dict[str, dict[str, object]]:
    return {volume["target"]: volume for volume in service.get("volumes", [])}


def test_gateway_is_persistent_userspace_and_unpublished() -> None:
    gateway = _rendered()["services"]["tailscale-gateway"]

    assert gateway["image"] == TAILSCALE_IMAGE
    assert gateway["read_only"] is True
    assert not gateway.get("ports")
    assert not gateway.get("devices")
    assert not gateway.get("cap_add")
    assert set(gateway["networks"]) == {"tailnet-hermes-edge", "tailnet-web-edge"}
    assert gateway["environment"] == {
        "TS_AUTH_ONCE": "true",
        "TS_CLIENT_ID": "file:/run/secrets/tailscale-oauth-client-id",
        "TS_CLIENT_SECRET": "file:/run/secrets/tailscale-oauth-client-secret",
        "TS_EXTRA_ARGS": "--advertise-tags=tag:vonk-gateway",
        "TS_HOSTNAME": "vonk-forge-gateway",
        "TS_SOCKET": "/var/run/tailscale/tailscaled.sock",
        "TS_STATE_DIR": "/var/lib/tailscale",
        "TS_USERSPACE": "true",
    }
    volumes = _volume_targets(gateway)
    assert volumes["/var/lib/tailscale"]["type"] == "volume"
    assert volumes["/var/run/tailscale"]["type"] == "volume"
    assert {secret["target"] for secret in gateway["secrets"]} == {
        "/run/secrets/tailscale-oauth-client-id",
        "/run/secrets/tailscale-oauth-client-secret",
    }


def test_configurator_waits_for_every_exact_backend_health_gate() -> None:
    configurator = _rendered()["services"]["tailscale-configurator"]

    assert configurator["image"] == TAILSCALE_IMAGE
    assert configurator["network_mode"] == "service:tailscale-gateway"
    assert configurator["read_only"] is True
    assert not configurator.get("ports")
    assert not configurator.get("networks")
    assert not configurator.get("devices")
    assert not configurator.get("cap_add")
    volumes = _volume_targets(configurator)
    assert volumes["/var/run/tailscale"]["type"] == "volume"
    assert volumes["/usr/local/bin/configure-tailscale"]["read_only"] is True
    assert configurator["restart"] == "unless-stopped"
    assert configurator["depends_on"] == {
        "caddy": {"condition": "service_healthy", "required": True, "restart": True},
        "hermes-agent": {"condition": "service_healthy", "required": False, "restart": True},
        "tailscale-gateway": {"condition": "service_healthy", "required": True, "restart": True},
    }


def test_service_map_and_configurator_are_exact_https_and_fail_closed() -> None:
    script = COMPOSE / "tailscale/configure.sh"
    subprocess.run(["/bin/sh", "-n", script], check=True)
    text = script.read_text()

    assert "serve set-config" not in text
    for command in (
        "--service=svc:vonk-forge --https=443 http://caddy:8080",
        "--service=svc:hermes-api --https=443 http://hermes-agent:8642",
        "--service=svc:hermes-dashboard --https=443 http://hermes-agent:9119",
    ):
        assert command in text
    for service in EXPECTED_MAP["services"]:
        assert f"serve advertise {service}" in text
    assert "serve get-config --all" in text
    assert "serve reset" in text
    assert json.dumps(EXPECTED_MAP, sort_keys=True, separators=(",", ":")) in text
    assert text.count('"HTTPS":true') >= 3
    assert '"HTTP":true' in text
    assert "120" in text
    assert "service-host" in text
    for forbidden in ("svc:*", "svc:ai-devbox", "tcp:22", "--tcp=22"):
        assert forbidden not in text


def test_configurator_repairs_plaintext_or_extra_service_map(tmp_path: Path) -> None:
    socket_path = tmp_path / "tailscaled.sock"
    daemon_socket = socket.socket(socket.AF_UNIX)
    daemon_socket.bind(str(socket_path))
    log = tmp_path / "calls.log"
    repaired = tmp_path / "repaired"
    status_checks = tmp_path / "status-checks"
    fake = tmp_path / "tailscale"
    healthy_status = json.dumps({
        "Services": {
            service: {"TCP": {"443": {"HTTPS": True}}}
            for service in EXPECTED_MAP["services"]
        }
    }, separators=(",", ":"))
    fake.write_text(
        "#!/bin/sh\n"
        f"log={log}\n"
        f"repaired={repaired}\n"
        f"status_checks={status_checks}\n"
        "case \"$*\" in\n"
        "  *\"serve get-config --all\"*)\n"
        "    if [ -f \"$repaired\" ]; then printf '%s\\n' "
        "'{\"version\":\"0.0.1\",\"services\":{\"svc:hermes-api\":{\"endpoints\":{\"tcp:443\":\"http://hermes-agent:8642\"}},\"svc:hermes-dashboard\":{\"endpoints\":{\"tcp:443\":\"http://hermes-agent:9119\"}},\"svc:vonk-forge\":{\"endpoints\":{\"tcp:443\":\"http://caddy:8080\"}}}}'; "
        "else printf '%s\\n' '{\"version\":\"0.0.1\",\"services\":{\"svc:extra\":{\"endpoints\":{\"tcp:99\":\"tcp://unexpected:99\"}}}}'; fi ;;\n"
        "  *\"serve status --json\"*)\n"
        "    if [ -f \"$repaired\" ]; then count=0; "
        "[ ! -f \"$status_checks\" ] || count=$(cat \"$status_checks\"); "
        "count=$((count + 1)); printf '%s\\n' \"$count\" >\"$status_checks\"; "
        f"if [ \"$count\" -le 2 ]; then printf '%s\\n' '{{\"Services\":{{}}}}'; else printf '%s\\n' '{healthy_status}'; fi; "
        "else printf '%s\\n' '{\"Services\":{\"svc:vonk-forge\":{\"TCP\":{\"443\":{\"HTTP\":true}}}}}'; fi ;;\n"
        "  *\"--service=svc:vonk-forge --https=443 http://caddy:8080\"*)\n"
        "    printf '%s\\n' \"$*\" >>\"$log\"; touch \"$repaired\" ;;\n"
        "  *\"status --json\"*) printf '%s\\n' '{\"Self\":{\"CapMap\":{\"services/vonk-forge\":[],\"services/hermes-api\":[],\"services/hermes-dashboard\":[]}}}' ;;\n"
        "  *) printf '%s\\n' \"$*\" >>\"$log\" ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    try:
        result = subprocess.run(
            ["/bin/sh", COMPOSE / "tailscale/configure.sh"],
            env=os.environ
            | {
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "TS_CONFIGURE_ONCE": "1",
                "TS_SOCKET_PATH": str(socket_path),
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        daemon_socket.close()

    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    for command in (
        "--service=svc:vonk-forge --https=443 http://caddy:8080",
        "--service=svc:hermes-api --https=443 http://hermes-agent:8642",
        "--service=svc:hermes-dashboard --https=443 http://hermes-agent:9119",
        "serve reset",
    ):
        assert command in calls
    assert "set-config" not in calls


def test_grants_example_is_exact_service_least_privilege() -> None:
    policy = json.loads((COMPOSE / "tailscale/grants.example.hujson").read_text())

    assert policy["tagOwners"] == {"tag:vonk-gateway": ["autogroup:admin"]}
    assert policy["groups"] == {
        "group:hermes-users": ["replace-with-your-login@github"]
    }
    assert policy["acls"] == []
    assert policy["grants"] == [
        {
            "src": ["autogroup:admin"],
            "dst": ["svc:vonk-forge"],
            "ip": ["tcp:443"],
        },
        {
            "src": ["group:hermes-users"],
            "dst": ["svc:hermes-api", "svc:hermes-dashboard"],
            "ip": ["tcp:443"],
        },
    ]
    assert policy["autoApprovers"] == {
        "services": {
            service: ["tag:vonk-gateway"] for service in EXPECTED_MAP["services"]
        }
    }
    assert policy["tests"] == [
        {"src": "autogroup:admin", "accept": ["svc:vonk-forge:443"]},
        {"src": "autogroup:member", "deny": ["svc:vonk-forge:443"]},
        {
            "src": "replace-with-your-login@github",
            "accept": ["svc:hermes-api:443", "svc:hermes-dashboard:443"],
        },
        {
            "src": "autogroup:member",
            "deny": ["svc:hermes-api:443", "svc:hermes-dashboard:443"],
        },
    ]
    rendered = json.dumps(policy)
    for forbidden in ("svc:*", "svc:ai-devbox", "tcp:22", "tskey-"):
        assert forbidden not in rendered.lower()
