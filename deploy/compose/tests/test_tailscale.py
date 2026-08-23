from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

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


DEFAULT_MAP = {
    "version": "0.0.1",
    "services": {
        "svc:vonk-forge": {"endpoints": {"tcp:443": "http://caddy:8080"}},
    },
}
HERMES_MAP = {
    "version": "0.0.1",
    "services": {
        "svc:vonk-forge": {"endpoints": {"tcp:443": "http://caddy:8080"}},
        "svc:hermes-api": {"endpoints": {"tcp:443": "http://hermes-agent:8642"}},
        "svc:hermes-dashboard": {"endpoints": {"tcp:443": "http://hermes-agent:9119"}},
    },
}


def _host_status(*services: str) -> str:
    mappings = {
        service: [f"100.64.0.{index}"]
        for index, service in enumerate(services, start=1)
    }
    return json.dumps(
        {
            "Self": {
                "CapMap": {"service-host": [mappings]},
                "PrimaryRoutes": [
                    f"{address}/32"
                    for addresses in mappings.values()
                    for address in addresses
                ],
            }
        },
        separators=(",", ":"),
    )


def _write_stateful_tailscale(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "tailscale-state.json"
    calls = tmp_path / "tailscale-calls.log"
    fake = tmp_path / "tailscale"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys, time\n"
        "state = pathlib.Path(os.environ['TS_TEST_STATE'])\n"
        "calls = pathlib.Path(os.environ['TS_TEST_CALLS'])\n"
        "args = [arg for arg in sys.argv[1:] if not arg.startswith('--socket=')]\n"
        "with calls.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(' '.join(args) + '\\n')\n"
        "def empty_state():\n"
        "    return {'config': {'version': '0.0.1', 'services': {}}, 'advertised': []}\n"
        "def load():\n"
        "    if not state.exists():\n"
        "        return empty_state()\n"
        "    return json.loads(state.read_text(encoding='utf-8'))\n"
        "def save(value):\n"
        "    temporary = state.with_name(state.name + '.' + str(os.getpid()))\n"
        "    temporary.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')), encoding='utf-8')\n"
        "    temporary.replace(state)\n"
        "if args == ['status', '--json']:\n"
        "    value = load()\n"
        "    mappings = {name: [f'100.64.0.{index}'] for index, name in enumerate(value['advertised'], start=1)}\n"
        "    routes = [addresses[0] + '/32' for addresses in mappings.values()]\n"
        "    print(json.dumps({'Self': {'CapMap': {'service-host': [mappings]}, 'PrimaryRoutes': routes}}, separators=(',', ':')))\n"
        "elif args == ['serve', 'get-config', '--all']:\n"
        "    time.sleep(float(os.environ.get('TS_TEST_READ_DELAY', '0')))\n"
        "    value = load()\n"
        "    config = value['config']\n"
        "    for name, details in config['services'].items():\n"
        "        if name not in value['advertised']:\n"
        "            details['advertised'] = False\n"
        "    print(json.dumps(config, sort_keys=True, separators=(',', ':')))\n"
        "elif args == ['serve', 'status', '--json']:\n"
        "    time.sleep(float(os.environ.get('TS_TEST_READ_DELAY', '0')))\n"
        "    services = {name: {'TCP': {'443': {'HTTPS': True}}} for name in load()['config']['services']}\n"
        "    print(json.dumps({'Services': services}, sort_keys=True, separators=(',', ':')))\n"
        "elif args == ['serve', 'reset']:\n"
        "    value = load()\n"
        "    value['config'] = {'version': '0.0.1', 'services': {}}\n"
        "    save(value)\n"
        "elif len(args) == 3 and args[:2] == ['serve', 'drain']:\n"
        "    value = load()\n"
        "    value['advertised'] = [name for name in value['advertised'] if name != args[2]]\n"
        "    save(value)\n"
        "elif len(args) == 3 and args[:2] == ['serve', 'clear']:\n"
        "    value = load()\n"
        "    if args[2] in value['config']['services']:\n"
        "        del value['config']['services'][args[2]]\n"
        "        value['advertised'] = [name for name in value['advertised'] if name != args[2]]\n"
        "        save(value)\n"
        "    else:\n"
        "        print(f'service {args[2]} not found in serve config, nothing to clear', file=sys.stderr)\n"
        "elif len(args) == 4 and args[:3] == ['serve', 'set-config', '--all']:\n"
        "    config = json.loads(pathlib.Path(args[3]).read_text(encoding='utf-8'))\n"
        "    advertised = [name for name, details in config['services'].items() if details.get('advertised', True)]\n"
        "    for details in config['services'].values():\n"
        "        details.pop('advertised', None)\n"
        "    save({'config': config, 'advertised': sorted(advertised)})\n"
        "elif len(args) == 3 and args[:2] == ['serve', 'advertise']:\n"
        "    value = load()\n"
        "    value['advertised'] = sorted(set(value['advertised']) | {args[2]})\n"
        "    save(value)\n"
        "elif args and args[0] == 'serve' and any(arg.startswith('--service=') for arg in args):\n"
        "    service = next(arg.split('=', 1)[1] for arg in args if arg.startswith('--service='))\n"
        "    upstream = args[-1]\n"
        "    value = load()\n"
        "    value['config']['services'][service] = {'endpoints': {'tcp:443': upstream}}\n"
        "    save(value)\n"
        "    print(f'Available within your tailnet: https://{service[4:]}')\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return state, calls


def _write_hermes_http_nc(tmp_path: Path) -> Path:
    requests = tmp_path / "hermes-http-requests.log"
    fake = tmp_path / "nc"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "request = sys.stdin.read()\n"
        "with pathlib.Path(os.environ['HERMES_TEST_REQUESTS']).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(request.replace('\\r', '<CR>') + '\\n---\\n')\n"
        "ready = pathlib.Path(os.environ['HERMES_TEST_READY']).exists()\n"
        "expected = 'Authorization: Bearer ' + os.environ['HERMES_TEST_KEY'] + '\\r\\n'\n"
        "if request.startswith('GET /health HTTP/1.1\\r\\n'):\n"
        "    status = 200 if ready and expected in request else (401 if expected not in request else 503)\n"
        "elif request.startswith('GET / HTTP/1.1\\r\\n'):\n"
        "    status = 200 if ready else 503\n"
        "else:\n"
        "    sys.exit(0)\n"
        "reason = 'OK' if status == 200 else 'Unavailable'\n"
        "sys.stdout.write(f'HTTP/1.1 {status} {reason}\\r\\nContent-Length: 0\\r\\n\\r\\n')\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return requests


def _write_logging_mktemp(tmp_path: Path) -> Path:
    allocations = tmp_path / "mktemp-allocations.log"
    fake = tmp_path / "mktemp"
    fake.write_text(
        "#!/bin/sh\n"
        'result=$(/usr/bin/mktemp "$@") || exit $?\n'
        'printf \'%s\\n\' "$result" >>"$TS_TEST_MKTEMP_ALLOCATIONS"\n'
        "printf '%s\\n' \"$result\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return allocations


def _wait_for_service_state(
    state: Path,
    expected: dict[str, object],
    advertised: set[str],
) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            actual = json.loads(state.read_text(encoding="utf-8"))
            if actual == {
                "config": expected,
                "advertised": sorted(advertised),
            }:
                return
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    actual = state.read_text(encoding="utf-8") if state.exists() else "<missing>"
    pytest.fail(f"Tailscale service state did not converge: {actual}")


def test_default_gateway_reconciles_only_the_vonk_service(tmp_path: Path) -> None:
    socket_path = tmp_path / "tailscaled.sock"
    daemon_socket = socket.socket(socket.AF_UNIX)
    daemon_socket.bind(str(socket_path))
    calls = tmp_path / "calls.log"
    fake = tmp_path / "tailscale"
    nc = tmp_path / "nc"
    nc.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    nc.chmod(0o755)
    expected = json.dumps(DEFAULT_MAP, separators=(",", ":"))
    host_status = _host_status("svc:vonk-forge")
    fake.write_text(
        "#!/bin/sh\n"
        f"calls={calls}\n"
        'printf \'%s\\n\' "$*" >>"$calls"\n'
        'case "$*" in\n'
        '  *"serve get-config --all"*) '
        f"printf '%s\\n' '{expected}' ;;\n"
        "  *\"serve status --json\"*) printf '%s\\n' "
        '\'{"Services":{"svc:vonk-forge":{"TCP":{"443":{"HTTPS":true}}}}}\' ;;\n'
        "  *\"status --json\"*) printf '%s\\n' "
        f"'{host_status}' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    try:
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
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            pytest.fail("default-only Tailscale service map did not converge")
    finally:
        daemon_socket.close()

    assert result.returncode == 0, result.stderr
    invocation_log = calls.read_text(encoding="utf-8")
    assert "serve get-config --all" in invocation_log
    assert "--service=svc:hermes-api" not in invocation_log
    assert "--service=svc:hermes-dashboard" not in invocation_log


def test_unavailable_hermes_with_stale_advertisements_is_withdrawn_before_exact_return(
    tmp_path: Path,
) -> None:
    """Catches an exact Serve map hiding stale Hermes AdvertiseServices."""
    socket_path = tmp_path / "tailscaled.sock"
    daemon_socket = socket.socket(socket.AF_UNIX)
    daemon_socket.bind(str(socket_path))
    state, calls = _write_stateful_tailscale(tmp_path)
    state.write_text(
        json.dumps(
            {
                "config": DEFAULT_MAP,
                "advertised": [
                    "svc:hermes-api",
                    "svc:hermes-dashboard",
                    "svc:vonk-forge",
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    nc = tmp_path / "nc"
    nc.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    nc.chmod(0o755)
    try:
        result = subprocess.run(
            ["/bin/sh", COMPOSE / "tailscale/configure.sh"],
            env=os.environ
            | {
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "TS_CONFIGURE_ONCE": "1",
                "TS_SOCKET_PATH": str(socket_path),
                "TS_TEST_STATE": str(state),
                "TS_TEST_CALLS": str(calls),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    finally:
        daemon_socket.close()

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "config": DEFAULT_MAP,
        "advertised": ["svc:vonk-forge"],
    }
    invocations = calls.read_text(encoding="utf-8").splitlines()
    first_exact_read = invocations.index("serve status --json")
    assert invocations.index("serve drain svc:hermes-api") < first_exact_read
    assert invocations.index("serve drain svc:hermes-dashboard") < first_exact_read


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    for line in (COMPOSE / "tests/test.env").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            environment[key] = value
    return environment


def _rendered(*, environment: dict[str, str] | None = None) -> dict[str, object]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment or _environment(),
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
    assert set(gateway["networks"]) == {
        "tailnet-control-plane",
        "tailnet-hermes-edge",
        "tailnet-web-edge",
    }
    assert gateway["environment"] == {
        "TS_AUTH_ONCE": "true",
        "TS_CLIENT_ID": "file:/run/secrets/tailscale-oauth-client-id",
        "TS_EXTRA_ARGS": "--advertise-tags=tag:vonk-gateway",
        "TS_HOSTNAME": "vonk-forge-gateway",
        "TS_SOCKET": "/var/run/tailscale/tailscaled.sock",
        "TS_STATE_DIR": "/var/lib/tailscale",
        "TS_USERSPACE": "true",
        "VONK_TAILSCALE_EPHEMERAL": "false",
    }
    assert gateway["command"][:3] == ["/bin/sh", "-eu", "-c"]
    bootstrap = gateway["command"][3]
    assert "printf '?ephemeral=%s&preauthorized=true'" in bootstrap
    assert '"$${VONK_TAILSCALE_EPHEMERAL}"' in bootstrap
    assert "TS_CLIENT_SECRET=file:/tmp/tailscale-oauth-client-secret" in bootstrap
    assert "exec env" in bootstrap
    assert "tr -d '\\r\\n'" in bootstrap
    assert "echo" not in bootstrap
    volumes = _volume_targets(gateway)
    assert volumes["/var/lib/tailscale"]["type"] == "volume"
    assert volumes["/var/run/tailscale"]["type"] == "volume"
    assert {secret["target"] for secret in gateway["secrets"]} == {
        "/run/secrets/tailscale-oauth-client-id",
        "/run/secrets/tailscale-oauth-client-secret",
    }


def test_gateway_can_be_ephemeral_for_acceptance() -> None:
    environment = _environment()
    environment["VONK_TAILSCALE_EPHEMERAL"] = "true"
    rendered = _rendered(environment=environment)

    assert (
        rendered["services"]["tailscale-gateway"]["environment"][
            "VONK_TAILSCALE_EPHEMERAL"
        ]
        == "true"
    )


def test_gateway_hostname_can_be_isolated_for_acceptance() -> None:
    environment = _environment()
    environment["VONK_TAILSCALE_GATEWAY_HOSTNAME"] = "vonk-forge-ci-123-1"
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    gateway = json.loads(result.stdout)["services"]["tailscale-gateway"]
    assert gateway["hostname"] == "vonk-forge-ci-123-1"
    assert gateway["environment"]["TS_HOSTNAME"] == "vonk-forge-ci-123-1"


def test_configurator_discovers_optional_hermes_without_a_profile_dependency() -> None:
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
    assert {secret["target"] for secret in configurator["secrets"]} == {
        "/run/secrets/hermes-api-key"
    }
    assert configurator["restart"] == "unless-stopped"
    assert configurator["environment"] == {
        "VONK_SELECTED_PROFILES": "",
        "VONK_TAILSCALE_CONTROL_SERVICE": "svc:vonk-forge",
        "VONK_TAILSCALE_HERMES_API_SERVICE": "svc:hermes-api",
        "VONK_TAILSCALE_HERMES_DASHBOARD_SERVICE": "svc:hermes-dashboard",
    }
    assert configurator["healthcheck"]["timeout"] == "8s"
    assert configurator["depends_on"] == {
        "caddy": {"condition": "service_healthy", "required": True, "restart": True},
        "tailscale-gateway": {
            "condition": "service_healthy",
            "required": True,
            "restart": True,
        },
    }


def test_service_map_and_configurator_are_exact_https_and_fail_closed() -> None:
    script = COMPOSE / "tailscale/configure.sh"
    subprocess.run(["/bin/sh", "-n", script], check=True)
    text = script.read_text()

    assert "serve set-config --all" in text
    for command in (
        '--service="${control_service}" --https=443 http://caddy:8080',
        '--service="${hermes_api_service}" --https=443 http://hermes-agent:8642',
        '--service="${hermes_dashboard_service}" --https=443 http://hermes-agent:9119',
        'serve advertise "${control_service}"',
        'serve advertise "${hermes_api_service}"',
        'serve advertise "${hermes_dashboard_service}"',
    ):
        assert command in text
    assert "serve get-config --all" in text
    assert "serve reset" not in text
    assert "VONK_TAILSCALE_CONTROL_SERVICE:-svc:vonk-forge" in text
    assert "VONK_TAILSCALE_HERMES_API_SERVICE:-svc:hermes-api" in text
    assert "VONK_TAILSCALE_HERMES_DASHBOARD_SERVICE:-svc:hermes-dashboard" in text
    assert "validate_service_name" in text
    assert text.count('HTTPS\\":true') >= 3
    assert '"HTTP":true' in text
    assert "120" in text
    assert "extract_json_array service-host" in text
    assert "PrimaryRoutes" in text
    assert "grant tag:vonk-gateway TCP 443 access" in text
    assert "tailscale-service-hosts.compact" in text
    assert '${hermes_api_service}' in text
    assert '${hermes_dashboard_service}' in text
    assert '"services/vonk-forge"' not in text
    assert "BusyBox nc exits as soon as piped stdin reaches EOF" in text
    assert "sleep 1" in text
    assert "'[23][0-9][0-9]'" in text
    health = text.split('if [ "${TS_HEALTHCHECK_ONLY:-0}" = "1" ]; then', 1)[1].split(
        "\nfi", 1
    )[0]
    assert "*,hermes,*)" in health
    assert "hermes_is_available && serve_is_exact 1" in health
    assert "serve_is_exact 0" in health
    for forbidden in ("svc:*", "svc:ai-devbox", "tcp:22", "--tcp=22"):
        assert forbidden not in text


def test_selected_hermes_profile_is_passed_to_the_configurator() -> None:
    environment = _environment()
    environment["COMPOSE_PROFILES"] = "hermes"
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    configurator = json.loads(result.stdout)["services"]["tailscale-configurator"]
    assert configurator["environment"] == {
        "VONK_SELECTED_PROFILES": "hermes",
        "VONK_TAILSCALE_CONTROL_SERVICE": "svc:vonk-forge",
        "VONK_TAILSCALE_HERMES_API_SERVICE": "svc:hermes-api",
        "VONK_TAILSCALE_HERMES_DASHBOARD_SERVICE": "svc:hermes-dashboard",
    }


def test_configurator_uses_distinct_overridden_service_names(tmp_path: Path) -> None:
    socket_path = tmp_path / "tailscaled.sock"
    daemon_socket = socket.socket(socket.AF_UNIX)
    daemon_socket.bind(str(socket_path))
    state, calls = _write_stateful_tailscale(tmp_path)
    nc = tmp_path / "nc"
    nc.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    nc.chmod(0o755)
    try:
        result = subprocess.run(
            ["/bin/sh", COMPOSE / "tailscale/configure.sh"],
            env=os.environ
            | {
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "TS_CONFIGURE_ONCE": "1",
                "TS_SOCKET_PATH": str(socket_path),
                "TS_TEST_STATE": str(state),
                "TS_TEST_CALLS": str(calls),
                "VONK_TAILSCALE_CONTROL_SERVICE": "svc:vonk-forge-acceptance",
                "VONK_TAILSCALE_HERMES_API_SERVICE": "svc:hermes-api-acceptance",
                "VONK_TAILSCALE_HERMES_DASHBOARD_SERVICE": "svc:hermes-dashboard-acceptance",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    finally:
        daemon_socket.close()

    assert result.returncode == 0, result.stderr
    assert json.loads(state.read_text(encoding="utf-8")) == {
        "config": {
            "version": "0.0.1",
            "services": {
                "svc:vonk-forge-acceptance": {
                    "endpoints": {"tcp:443": "http://caddy:8080"}
                }
            },
        },
        "advertised": ["svc:vonk-forge-acceptance"],
    }
    invocations = calls.read_text(encoding="utf-8")
    assert "--service=svc:vonk-forge-acceptance" in invocations
    assert "serve drain svc:hermes-api-acceptance" in invocations
    assert "serve drain svc:hermes-dashboard-acceptance" in invocations


@pytest.mark.parametrize(
    "environment",
    [
        {"VONK_TAILSCALE_CONTROL_SERVICE": "svc:Uppercase"},
        {
            "VONK_TAILSCALE_CONTROL_SERVICE": "svc:duplicate",
            "VONK_TAILSCALE_HERMES_API_SERVICE": "svc:duplicate",
        },
    ],
)
def test_configurator_rejects_invalid_or_duplicate_service_names(
    environment: dict[str, str],
) -> None:
    result = subprocess.run(
        ["/bin/sh", COMPOSE / "tailscale/configure.sh"],
        env=os.environ | environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 64
    assert "Tailscale Service names" in result.stderr


def test_unapproved_advertisement_does_not_claim_service_host_readiness(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "tailscaled.sock"
    daemon_socket = socket.socket(socket.AF_UNIX)
    daemon_socket.bind(str(socket_path))
    fake = tmp_path / "tailscale"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *\"status --json\"*) printf '%s\\n' "
        '\'{"Self":{"AdvertisedServices":["svc:vonk-forge"],"CapMap":{"service-host":[{}],"services/vonk-forge":[{}]},"PrimaryRoutes":["100.70.230.202/32"]}}\' ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    try:
        result = subprocess.run(
            ["/bin/sh", COMPOSE / "tailscale/configure.sh"],
            env=os.environ
            | {
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "TS_HEALTHCHECK_ONLY": "1",
                "TS_SOCKET_PATH": str(socket_path),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    finally:
        daemon_socket.close()

    assert result.returncode != 0


def test_service_host_mapping_without_primary_routes_is_unhealthy(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "tailscaled.sock"
    daemon_socket = socket.socket(socket.AF_UNIX)
    daemon_socket.bind(str(socket_path))
    fake = tmp_path / "tailscale"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"status --json"*) printf \'%s\\n\' '
        "'{\"Self\":{\"CapMap\":{\"service-host\":[{\"svc:vonk-forge\":[\"100.70.230.202\"]}]},\"PrimaryRoutes\":[]}}' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    try:
        result = subprocess.run(
            ["/bin/sh", COMPOSE / "tailscale/configure.sh"],
            env=os.environ
            | {
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "TS_HEALTHCHECK_ONLY": "1",
                "TS_SOCKET_PATH": str(socket_path),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    finally:
        daemon_socket.close()

    assert result.returncode != 0


def test_configurator_readvertises_mapped_service_without_primary_route(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "tailscaled.sock"
    daemon_socket = socket.socket(socket.AF_UNIX)
    daemon_socket.bind(str(socket_path))
    repaired = tmp_path / "repaired"
    fake = tmp_path / "tailscale"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"repaired = pathlib.Path({os.fspath(repaired)!r})\n"
        "args = [value for value in sys.argv[1:] if not value.startswith('--socket=')]\n"
        "if args == ['status', '--json']:\n"
        "    routes = ['100.70.230.202/32'] if repaired.exists() else []\n"
        "    print(json.dumps({'Self': {'CapMap': {'service-host': [{'svc:vonk-forge': ['100.70.230.202']}]}, 'PrimaryRoutes': routes}}, separators=(',', ':')))\n"
        "elif args == ['serve', 'status', '--json']:\n"
        "    print(json.dumps({'Services': {'svc:vonk-forge': {'TCP': {'443': {'HTTPS': True}}}}}, separators=(',', ':')))\n"
        "elif args == ['serve', 'get-config', '--all']:\n"
        f"    print({json.dumps(DEFAULT_MAP, separators=(',', ':'))!r})\n"
        "elif args[:3] == ['serve', 'set-config', '--all']:\n"
        "    repaired.touch()\n",
        encoding="utf-8",
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
            timeout=5,
        )
    finally:
        daemon_socket.close()

    assert result.returncode == 0, result.stderr
    assert repaired.is_file()


def test_configurator_repairs_plaintext_or_extra_service_map(tmp_path: Path) -> None:
    socket_path = tmp_path / "tailscaled.sock"
    daemon_socket = socket.socket(socket.AF_UNIX)
    daemon_socket.bind(str(socket_path))
    log = tmp_path / "calls.log"
    repaired = tmp_path / "repaired"
    status_checks = tmp_path / "status-checks"
    fake = tmp_path / "tailscale"
    nc = tmp_path / "nc"
    nc.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    nc.chmod(0o755)
    healthy_status = json.dumps(
        {
            "Services": {
                service: {"TCP": {"443": {"HTTPS": True}}}
                for service in DEFAULT_MAP["services"]
            }
        },
        separators=(",", ":"),
    )
    host_status = _host_status("svc:vonk-forge")
    fake.write_text(
        "#!/bin/sh\n"
        f"log={log}\n"
        f"repaired={repaired}\n"
        f"status_checks={status_checks}\n"
        'case "$*" in\n'
        '  *"serve get-config --all"*)\n'
        "    if [ -f \"$repaired\" ]; then printf '%s\\n' "
        '\'{"version":"0.0.1","services":{"svc:vonk-forge":{"endpoints":{"tcp:443":"http://caddy:8080"}}}}\'; '
        'else printf \'%s\\n\' \'{"version":"0.0.1","services":{"svc:extra":{"endpoints":{"tcp:99":"tcp://unexpected:99"}}}}\'; fi ;;\n'
        '  *"serve status --json"*)\n'
        '    if [ -f "$repaired" ]; then count=0; '
        '[ ! -f "$status_checks" ] || count=$(cat "$status_checks"); '
        'count=$((count + 1)); printf \'%s\\n\' "$count" >"$status_checks"; '
        f"if [ \"$count\" -le 2 ]; then printf '%s\\n' '{{\"Services\":{{}}}}'; else printf '%s\\n' '{healthy_status}'; fi; "
        'else printf \'%s\\n\' \'{"Services":{"svc:vonk-forge":{"TCP":{"443":{"HTTP":true}}}}}\'; fi ;;\n'
        '  *"--service=svc:vonk-forge --https=443 http://caddy:8080"*)\n'
        '    printf \'%s\\n\' "$*" >>"$log"; touch "$repaired" ;;\n'
        f"  *\"status --json\"*) printf '%s\\n' '{host_status}' ;;\n"
        '  *) printf \'%s\\n\' "$*" >>"$log" ;;\n'
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
        "serve set-config --all",
    ):
        assert command in calls
    assert "--service=svc:hermes-" not in calls
    assert "serve reset" not in calls


def test_configurator_advertises_hermes_when_both_profile_endpoints_are_available(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "tailscaled.sock"
    daemon_socket = socket.socket(socket.AF_UNIX)
    daemon_socket.bind(str(socket_path))
    calls = tmp_path / "calls.log"
    repaired = tmp_path / "repaired"
    fake = tmp_path / "tailscale"
    requests = _write_hermes_http_nc(tmp_path)
    ready = tmp_path / "hermes-ready"
    ready.touch()
    key = tmp_path / "hermes-api-key"
    key.write_text("test-hermes-key\n", encoding="utf-8")
    expected_map = json.dumps(HERMES_MAP, sort_keys=True, separators=(",", ":"))
    healthy_status = json.dumps(
        {
            "Services": {
                service: {"TCP": {"443": {"HTTPS": True}}}
                for service in HERMES_MAP["services"]
            }
        },
        separators=(",", ":"),
    )
    host_status = _host_status(*HERMES_MAP["services"])
    fake.write_text(
        "#!/bin/sh\n"
        f"calls={calls}\n"
        f"repaired={repaired}\n"
        'printf \'%s\\n\' "$*" >>"$calls"\n'
        'case "$*" in\n'
        '  *"serve get-config --all"*) '
        f"if [ -f \"$repaired\" ]; then printf '%s\\n' '{expected_map}'; "
        'else printf \'%s\\n\' \'{"version":"0.0.1","services":{}}\'; fi ;;\n'
        '  *"serve status --json"*) '
        f"if [ -f \"$repaired\" ]; then printf '%s\\n' '{healthy_status}'; "
        "else printf '%s\\n' '{\"Services\":{}}'; fi ;;\n"
        '  *"--service=svc:vonk-forge --https=443 http://caddy:8080"*) '
        'touch "$repaired" ;;\n'
        "  *\"status --json\"*) printf '%s\\n' "
        f"'{host_status}' ;;\n"
        "esac\n",
        encoding="utf-8",
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
                "HERMES_API_KEY_PATH": str(key),
                "HERMES_TEST_KEY": "test-hermes-key",
                "HERMES_TEST_READY": str(ready),
                "HERMES_TEST_REQUESTS": str(requests),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        missing_hermes_capabilities = fake.read_text(encoding="utf-8").replace(
            host_status,
            _host_status("svc:vonk-forge"),
        )
        fake.write_text(missing_hermes_capabilities, encoding="utf-8")
        health = subprocess.run(
            ["/bin/sh", COMPOSE / "tailscale/configure.sh"],
            env=os.environ
            | {
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "TS_HEALTHCHECK_ONLY": "1",
                "TS_SOCKET_PATH": str(socket_path),
                "VONK_SELECTED_PROFILES": "hermes",
                "HERMES_API_KEY_PATH": str(key),
                "HERMES_TEST_KEY": "test-hermes-key",
                "HERMES_TEST_READY": str(ready),
                "HERMES_TEST_REQUESTS": str(requests),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    finally:
        daemon_socket.close()

    assert result.returncode == 0, result.stderr
    assert health.returncode != 0
    invocation_log = calls.read_text(encoding="utf-8")
    for service in HERMES_MAP["services"]:
        assert f"--service={service} --https=443" in invocation_log
    request_log = requests.read_text(encoding="utf-8")
    assert "GET /health HTTP/1.1<CR>" in request_log
    assert "Authorization: Bearer test-hermes-key<CR>" in request_log
    assert "GET / HTTP/1.1<CR>" in request_log


def test_reconciler_tracks_authenticated_hermes_readiness_and_is_concurrency_safe(
    tmp_path: Path,
) -> None:
    """Catches TCP-only readiness, stale advertisements, and shared scratch files."""
    socket_path = tmp_path / "tailscaled.sock"
    daemon_socket = socket.socket(socket.AF_UNIX)
    daemon_socket.bind(str(socket_path))
    state, calls = _write_stateful_tailscale(tmp_path)
    requests = _write_hermes_http_nc(tmp_path)
    allocations = _write_logging_mktemp(tmp_path)
    ready = tmp_path / "hermes-ready"
    key = tmp_path / "hermes-api-key"
    key.write_text("transition-key\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    environment = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TMPDIR": str(scratch),
        "TS_SOCKET_PATH": str(socket_path),
        "TS_RECONCILE_INTERVAL_SECONDS": "1",
        "TS_TEST_STATE": str(state),
        "TS_TEST_CALLS": str(calls),
        "TS_TEST_READ_DELAY": "0.05",
        "TS_TEST_MKTEMP_ALLOCATIONS": str(allocations),
        "HERMES_API_KEY_PATH": str(key),
        "HERMES_TEST_KEY": "transition-key",
        "HERMES_TEST_READY": str(ready),
        "HERMES_TEST_REQUESTS": str(requests),
    }
    reconciler = subprocess.Popen(
        ["/bin/sh", COMPOSE / "tailscale/configure.sh"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_service_state(state, DEFAULT_MAP, {"svc:vonk-forge"})
        ready.touch()
        _wait_for_service_state(state, HERMES_MAP, set(HERMES_MAP["services"]))
        ready.unlink()
        _wait_for_service_state(state, DEFAULT_MAP, {"svc:vonk-forge"})
        ready.touch()
        _wait_for_service_state(state, HERMES_MAP, set(HERMES_MAP["services"]))

        healthchecks = [
            subprocess.Popen(
                ["/bin/sh", COMPOSE / "tailscale/configure.sh"],
                env=environment | {"TS_HEALTHCHECK_ONLY": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        results = [healthcheck.communicate(timeout=8) for healthcheck in healthchecks]
        assert [healthcheck.returncode for healthcheck in healthchecks] == [
            0,
            0,
            0,
            0,
        ], results
        assert results == [("", "")] * 4
    finally:
        reconciler.terminate()
        try:
            stdout, stderr = reconciler.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            reconciler.kill()
            stdout, stderr = reconciler.communicate(timeout=5)
        daemon_socket.close()
    assert reconciler.returncode in {-15, 0}, (stdout, stderr)
    assert stdout == ""
    assert stderr == ""
    allocated = allocations.read_text(encoding="utf-8").splitlines()
    assert len(allocated) >= 5
    assert len(allocated) == len(set(allocated))
    assert not list(scratch.iterdir())
    invocations = calls.read_text(encoding="utf-8").splitlines()
    drain_api = invocations.index("serve drain svc:hermes-api")
    clear_api = invocations.index("serve clear svc:hermes-api", drain_api)
    drain_dashboard = invocations.index("serve drain svc:hermes-dashboard", clear_api)
    clear_dashboard = invocations.index(
        "serve clear svc:hermes-dashboard", drain_dashboard
    )
    clear_all = next(
        index
        for index, invocation in enumerate(
            invocations[clear_dashboard:], clear_dashboard
        )
        if invocation.startswith("serve set-config --all ")
    )
    assert drain_api < clear_api < drain_dashboard < clear_dashboard < clear_all


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
        {
            "src": ["tag:vonk-gateway"],
            "dst": ["svc:vonk-forge", "svc:hermes-api", "svc:hermes-dashboard"],
            "ip": ["tcp:443"],
        },
    ]
    assert policy["autoApprovers"] == {
        "services": {
            service: ["tag:vonk-gateway"] for service in HERMES_MAP["services"]
        }
    }
    assert policy["tests"] == [
        {
            "src": "replace-with-your-login@github",
            "accept": ["svc:hermes-api:443", "svc:hermes-dashboard:443"],
        },
        {
            "src": "tag:vonk-gateway",
            "accept": [
                "svc:vonk-forge:443",
                "svc:hermes-api:443",
                "svc:hermes-dashboard:443",
            ],
        },
    ]
    rendered = json.dumps(policy)
    for forbidden in ("svc:*", "svc:ai-devbox", "tcp:22", "tskey-"):
        assert forbidden not in rendered.lower()
