import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatchcase
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[3]
DEV_CADDYFILE = ROOT / "deploy/compose/Caddyfile"
DEV_CADDY_IMAGE = "caddy:2.11.4@sha256:0c994536bddb66445885237f1a5dcc1916bccea922661c76b4e9fc24061f9b52"


def _environment() -> dict[str, str]:
    return os.environ | {
        "CONTROL_API_IMAGE": "example/control-api:1@sha256:" + "c" * 64,
        "CONTROL_WORKER_IMAGE": "example/control-worker:1@sha256:" + "8" * 64,
        "HERMES_AGENT_IMAGE": "example/hermes:1@sha256:" + "7" * 64,
        "LITELLM_IMAGE": "example/litellm:1@sha256:" + "d" * 64,
        "VONK_CONTROL_HOSTNAME": "control.test.example",
        "VONK_MANAGEMENT_CIDRS": "10.0.0.0/24",
        "VONK_DIRECT_FABRIC_CIDRS": "192.168.100.0/24,192.168.101.0/24",
        "NAS_LAN_IP": "10.0.0.2",
        # The names the Caddy entrypoint derives from the control hostname,
        # for tests that adapt the Caddyfile without the entrypoint.
        "VONK_AGENT_ENROLL_HOSTNAME": "enroll.control.test.example",
        "VONK_AGENT_HOSTNAME": "agents.control.test.example",
        "VONK_REGISTRY_HOSTNAME": "registry.control.test.example",
    }


def _require_docker_runtime() -> None:
    result = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        if os.environ.get("CI"):
            pytest.fail("Docker daemon unavailable")
        pytest.skip("Docker daemon unavailable")


def _rendered(*files: str, environment: dict[str, str] | None = None) -> dict:
    command = ["docker", "compose"]
    for file in files or ("compose.yaml",):
        command.extend(("-f", str(ROOT / "deploy/compose" / file)))
    command.extend(("config", "--format", "json"))
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=environment or _environment(),
    )
    return json.loads(result.stdout)


def _adapted_caddy(environment: dict[str, str], caddyfile: str | None = None) -> dict:
    _require_docker_runtime()
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "-e",
            f"VONK_CONTROL_HOSTNAME={environment['VONK_CONTROL_HOSTNAME']}",
            "-e",
            f"VONK_AGENT_ENROLL_HOSTNAME={environment.get('VONK_AGENT_ENROLL_HOSTNAME', 'enroll.' + environment['VONK_CONTROL_HOSTNAME'])}",
            "-e",
            f"VONK_AGENT_HOSTNAME={environment.get('VONK_AGENT_HOSTNAME', 'agents.' + environment['VONK_CONTROL_HOSTNAME'])}",
            "-e",
            f"VONK_REGISTRY_HOSTNAME={environment.get('VONK_REGISTRY_HOSTNAME', 'registry.' + environment['VONK_CONTROL_HOSTNAME'])}",
            "-e",
            "VONK_AGENT_PROXY_AUTH=test-proxy-secret",
            DEV_CADDY_IMAGE,
            "caddy",
            "adapt",
            "--config",
            "-",
            "--adapter",
            "caddyfile",
        ],
        check=True,
        capture_output=True,
        text=True,
        input=(
            caddyfile
            if caddyfile is not None
            else (ROOT / "deploy/compose/Caddyfile").read_text()
        ),
    )
    return json.loads(result.stdout)


def _server_on_port(adapted: dict, port: int) -> dict:
    suffix = f":{port}"
    return next(
        server
        for server in adapted["apps"]["http"]["servers"].values()
        if any(str(listener).endswith(suffix) for listener in server.get("listen", []))
    )


def _request_body_routes(value: object) -> list[dict]:
    routes: list[dict] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            handlers = item.get("handle")
            if isinstance(handlers, list):
                for handler in handlers:
                    if (
                        isinstance(handler, dict)
                        and handler.get("handler") == "request_body"
                    ):
                        routes.append(
                            {
                                "match": item.get("match"),
                                "max_size": handler.get("max_size"),
                            }
                        )
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return routes


def _routes_with_handlers(value: object) -> list[dict]:
    routes: list[dict] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            if isinstance(item.get("handle"), list):
                routes.append(item)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return routes


def _assert_browser_sanitizer_precedes_each_upstream(value: object) -> None:
    expected_deletes = ["Forwarded", "X-Forwarded-*", "X-Vonk-Agent-*"]
    expected_upstreams = {
        "litellm:4000": 2,
        "grafana:3000": 1,
        "control-api:8000": 1,
    }

    for upstream, expected_count in expected_upstreams.items():
        sequences: list[list[dict]] = []
        for route in _routes_with_handlers(value):
            handlers = route["handle"]
            if any(
                handler.get("handler") == "reverse_proxy"
                and handler.get("upstreams") == [{"dial": upstream}]
                for handler in handlers
            ):
                sequences.append(handlers)
        assert len(sequences) == expected_count, (upstream, sequences)
        for handlers in sequences:
            proxy_index = next(
                index
                for index, handler in enumerate(handlers)
                if handler.get("handler") == "reverse_proxy"
                and handler.get("upstreams") == [{"dial": upstream}]
            )
            deleted_headers = [
                header
                for handler in handlers[:proxy_index]
                if handler.get("handler") == "headers"
                for header in handler.get("request", {}).get("delete", [])
            ]
            assert deleted_headers == expected_deletes


def _adapted_development_caddy() -> dict:
    _require_docker_runtime()
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "--user",
            "10000:10000",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:rw,mode=1777",
            "--tmpfs",
            "/run/vonk-caddy:rw,exec,mode=0700,uid=10000,gid=10000",
            "-e",
            "VONK_AGENT_ENROLL_HOSTNAME=enroll.test.example",
            "-e",
            "VONK_AGENT_HOSTNAME=agents.test.example",
            "-e",
            "VONK_CONTROL_HOSTNAME=vonk-forge.tailnet.test.ts.net",
            "-e",
            "VONK_BACKEND_PORT=8443",
            "-e",
            "VONK_MANAGEMENT_CIDRS=10.0.0.0/24",
            "--entrypoint",
            "/bin/sh",
            DEV_CADDY_IMAGE,
            "-c",
            (
                "printf '%s\\n' 'header_up X-Vonk-Agent-Proxy-Auth test' "
                ">/tmp/vonk-agent-proxy-auth.caddy; "
                "cp /usr/bin/caddy /run/vonk-caddy/caddy; "
                "chmod 0500 /run/vonk-caddy/caddy; "
                "exec /run/vonk-caddy/caddy adapt --config - --adapter caddyfile"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        input=DEV_CADDYFILE.read_text(encoding="utf-8"),
        timeout=30,
    )
    return json.loads(result.stdout)


def _entrypoint_result(
    environment: dict[str, str],
    secret_source: str | None = None,
    entrypoint_arguments: tuple[str, ...] = (),
    *,
    runtime_options: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    _require_docker_runtime()
    command = ["docker", "run", "--rm", *runtime_options]
    for name, value in environment.items():
        command.extend(("-e", f"{name}={value}"))
    command.extend(
        (
            "-v",
            f"{ROOT / 'deploy/compose/caddy/entrypoint.sh'}:/run/vonk-runtime-assets/caddy/entrypoint.sh:ro",
            "-v",
            "/etc/hostname:/run/secrets/controller-server-certificate:ro",
            "-v",
            "/etc/hostname:/run/secrets/controller-server-key:ro",
            "-v",
            "/etc/hostname:/run/secrets/agent-client-ca:ro",
        )
    )
    if secret_source is not None:
        command.extend(("-v", f"{secret_source}:/run/secrets/agent-proxy-auth:ro"))
    command.extend(
        (
            DEV_CADDY_IMAGE,
            "/bin/sh",
            "/run/vonk-runtime-assets/caddy/entrypoint.sh",
        )
    )
    command.extend(entrypoint_arguments)
    return subprocess.run(
        command, capture_output=True, text=True, timeout=10, check=False
    )


def _settings_result(
    rendered: dict, tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """Load the real Controller settings from the rendered API environment."""
    secrets = tmp_path / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    for name, value in {
        "database-url": "postgresql://control:pw@postgres/control\n",
        "agent-proxy-auth": "A" * 30 + "_-\r\n",
    }.items():
        (secrets / name).write_text(value)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("VONK_")
    } | {
        name: str(value)
        for name, value in rendered["services"]["control-api"]["environment"].items()
    }
    return subprocess.run(
        [
            # A fresh interpreter from the synced control environment: the
            # settings are read from this process environment only.
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; "
                "import vonk_control.settings as module; "
                "module.SECRETS_ROOT = Path(sys.argv[1]); "
                "settings = module.Settings.from_env_and_secrets(); "
                "print(settings.agent_proxy_auth.decode('ascii')); "
                "print(settings.management_cidrs); "
                "print(settings.direct_fabric_cidrs); "
                "print(settings.agent_enrollment_origin)"
            ),
            str(secrets),
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def test_development_image_compose_enables_complete_step_ca_agent_settings(
    tmp_path: Path,
) -> None:
    rendered = _rendered("compose.yaml")
    services = rendered["services"]
    api = services["control-api"]
    caddy = services["caddy"]

    result = _settings_result(rendered, tmp_path / "settings")
    assert result.returncode == 0, result.stderr
    proxy_auth, management, direct_fabric, enrollment = result.stdout.splitlines()
    assert proxy_auth == "A" * 30 + "_-"
    assert management == "10.0.0.0/24"
    assert direct_fabric == "192.168.100.0/24,192.168.101.0/24"
    assert enrollment == "https://enroll.control.test.example:8443"

    assert api["environment"]["VONK_NAS_LAN_IP"] == "10.0.0.2"
    assert set(caddy["networks"]) == {
        "agent-proxy",
        "hermes-inference",
        "ingress",
        "litellm-edge",
        "registry-edge",
        "tailnet-web-edge",
    }
    assert set(api["networks"]) == {
        "agent-proxy",
        "application",
        "ca",
        "data",
    }
    assert set(services["litellm"]["networks"]) == {
        "cluster-egress",
        "litellm-data",
        "litellm-edge",
    }
    assert services["litellm"].get("ports") in (None, [])
    # Caddy waits for its staged Caddyfile, never for a healthy Controller.
    assert caddy["depends_on"]["control-api"] == {
        "condition": "service_started",
        "required": False,
        "restart": True,
    }


def test_agent_bootstrap_uses_the_normalized_public_ca() -> None:
    rendered = _rendered()
    api = rendered["services"]["control-api"]
    api_secrets = {secret["source"] for secret in api.get("secrets", [])}

    assert "controller-ca" in api_secrets
    assert any(
        volume["target"] == "/run/vonk-normalized-secrets" for volume in api["volumes"]
    )
    assert "controller-server-key" not in api_secrets
    assert "agent-intermediate-key" not in api_secrets


def test_development_caddy_health_listener_is_exact_and_loopback_only() -> None:
    adapted = _adapted_development_caddy()
    health = _server_on_port(adapted, 8082)

    assert health["listen"] == ["127.0.0.1:8082"]
    assert health["routes"] == [
        {
            "match": [{"host": ["127.0.0.1"]}],
            "handle": [
                {
                    "handler": "subroute",
                    "routes": [
                        {
                            "handle": [
                                {
                                    "handler": "subroute",
                                    "routes": [
                                        {
                                            "handle": [
                                                {
                                                    "handler": "static_response",
                                                    "status_code": 200,
                                                }
                                            ]
                                        }
                                    ],
                                }
                            ],
                            "match": [{"path": ["/healthz"]}],
                        },
                        {
                            "handle": [
                                {"handler": "static_response", "status_code": 404}
                            ]
                        },
                    ],
                }
            ],
            "terminal": True,
        }
    ]
    assert "tls_connection_policies" not in health
    serialized = json.dumps(health, sort_keys=True)
    assert "reverse_proxy" not in serialized
    assert "control-api:8000" not in serialized
    assert "/agent/" not in serialized

    listeners = {
        listener
        for server in adapted["apps"]["http"]["servers"].values()
        for listener in server.get("listen", [])
    }
    assert "127.0.0.1:8082" in listeners
    assert ":2019" not in listeners
    assert "0.0.0.0:2019" not in listeners
    assert "[::]:2019" not in listeners


def test_recipe_library_relay_is_read_only_and_repository_scoped() -> None:
    adapted = _adapted_caddy(_environment())
    relay = _server_on_port(adapted, 8083)
    serialized = json.dumps(relay, sort_keys=True)

    assert relay["listen"] == [":8083"]
    assert '"method": ["GET"]' in serialized
    assert "/repos/CarstVaartjes/vonk-forge-recipes/*" in serialized
    assert "api.github.com:443" in serialized
    assert '"status_code": 404' in serialized
    assert "control-api:8000" not in serialized


def test_recipe_library_asset_relay_is_read_only_and_repository_scoped() -> None:
    adapted = _adapted_caddy(_environment())
    relay = _server_on_port(adapted, 8085)
    serialized = json.dumps(relay, sort_keys=True)

    assert relay["listen"] == [":8085"]
    assert '"method": ["GET"]' in serialized
    assert "path_regexp" in serialized
    assert "^/CarstVaartjes/vonk-forge-recipes/releases/download/" in serialized
    assert "^/github-production-release-asset/1336002555/" in serialized
    assert "github.com:443" in serialized
    assert "release-assets.githubusercontent.com:443" in serialized
    assert "raw.githubusercontent.com" not in serialized
    assert '"status_code": 404' in serialized
    assert "control-api:8000" not in serialized


def test_agent_release_relay_is_read_only_and_path_scoped() -> None:
    adapted = _adapted_caddy(_environment())
    relay = _server_on_port(adapted, 8084)
    serialized = json.dumps(relay, sort_keys=True)

    assert relay["listen"] == [":8084"]
    assert '"method": ["GET"]' in serialized
    assert "/artifacts/dev/current.manifest" in serialized
    assert "/artifacts/stable/current.manifest" in serialized
    assert "vonk-forge-agent.deb.host.sig" in serialized
    assert "install.vonkforge.ai:443" in serialized
    assert '"status_code": 404' in serialized
    assert "control-api:8000" not in serialized


def test_agent_package_relay_matches_only_digest_bound_package_documents() -> None:
    adapted = _adapted_caddy(_environment())
    relay = _server_on_port(adapted, 8084)
    package_route = next(
        route
        for route in _routes_with_handlers(relay["routes"])
        if any("path_regexp" in matcher for matcher in route.get("match", []))
    )
    matcher = next(
        matcher["path_regexp"]["pattern"]
        for matcher in package_route.get("match", [])
        if "path_regexp" in matcher
    )
    assert re.fullmatch(
        matcher,
        "/artifacts/dev/agent-builds/" + "a" * 64 + "/" + "b" * 64 + "/package.json",
    )
    for denied in (
        "/artifacts/dev/agent-builds/" + "a" * 63 + "/" + "b" * 64 + "/package.json",
        "/artifacts/dev/agent-builds/" + "a" * 64 + "/" + "b" * 63 + "/package.json",
        "/artifacts/test/agent-builds/" + "a" * 64 + "/" + "b" * 64 + "/package.json",
        "/artifacts/dev/agent-builds/" + "a" * 64 + "/" + "b" * 64 + "/other.json",
        "/artifacts/dev/agent-builds/"
        + "a" * 64
        + "/"
        + "b" * 64
        + "/package.json/extra",
    ):
        assert re.fullmatch(matcher, denied) is None
    assert package_route["match"][0]["method"] == ["GET"]
    assert "install.vonkforge.ai:443" in json.dumps(package_route)


def test_development_browser_edge_accepts_only_the_canonical_tailscale_service_host() -> (
    None
):
    adapted = _adapted_development_caddy()
    browser = _server_on_port(adapted, 8080)

    assert browser["listen"] == [":8080"]
    routes = _routes_with_handlers(browser["routes"])
    trusted = next(
        route
        for route in routes
        if route.get("match") == [{"host": ["vonk-forge.tailnet.test.ts.net"]}]
    )
    trusted_routes = trusted["handle"][0]["routes"]
    trusted_serialized = json.dumps(trusted_routes, sort_keys=True)

    assert '"max_size": 1000000' in trusted_serialized
    for header in (
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
    ):
        assert header in trusted_serialized
    for path, status in (
        ("/agent/*", 404),
        ("/internal/*", 404),
    ):
        route = next(
            candidate
            for candidate in routes
            if candidate.get("match") == [{"path": [path]}]
        )
        assert f'"status_code": {status}' in json.dumps(route, sort_keys=True)
    repository_authority = next(
        route
        for route in routes
        if route.get("match")
        == [
            {
                "method": ["POST", "PUT", "PATCH", "DELETE"],
                "path": [
                    "/litellm/model",
                    "/litellm/model/*",
                    "/litellm/model_group",
                    "/litellm/model_group/*",
                    "/litellm/config",
                    "/litellm/config/*",
                ],
            }
        ]
    )
    assert '"status_code": 403' in json.dumps(repository_authority, sort_keys=True)
    assert "litellm:4000" in trusted_serialized
    assert "control-api:8000" in trusted_serialized

    _assert_browser_sanitizer_precedes_each_upstream(trusted_routes)

    rejected = next(
        route
        for route in routes
        if "match" not in route and '"status_code": 421' in json.dumps(route)
    )
    assert '"status_code": 421' in json.dumps(rejected)


def test_production_browser_edge_accepts_only_control_hostname_and_fails_closed() -> (
    None
):
    environment = _environment()
    source = (ROOT / "deploy/compose/Caddyfile").read_text(encoding="utf-8")
    assert "@canonical_browser_host host {$VONK_CONTROL_HOSTNAME}" in source
    assert "respond 421" in source

    adapted = _adapted_caddy(environment)
    browser = _server_on_port(adapted, 8080)
    routes = _routes_with_handlers(browser["routes"])
    trusted = next(
        route
        for route in routes
        if route.get("match") == [{"host": [environment["VONK_CONTROL_HOSTNAME"]]}]
    )
    trusted_routes = trusted["handle"][0]["routes"]
    trusted_serialized = json.dumps(trusted_routes, sort_keys=True)

    assert "control-api:8000" in trusted_serialized
    assert "litellm:4000" in trusted_serialized
    assert "grafana:3000" in trusted_serialized
    trusted_adapter_routes = _routes_with_handlers(trusted_routes)
    for path in ("/agent/*", "/internal/*"):
        denied = next(
            route
            for route in trusted_adapter_routes
            if route.get("match") == [{"path": [path]}]
        )
        assert '"status_code": 404' in json.dumps(denied, sort_keys=True)
    repository_authority = next(
        route
        for route in trusted_adapter_routes
        if route.get("match")
        == [
            {
                "method": ["POST", "PUT", "PATCH", "DELETE"],
                "path": [
                    "/litellm/model",
                    "/litellm/model/*",
                    "/litellm/model_group",
                    "/litellm/model_group/*",
                    "/litellm/config",
                    "/litellm/config/*",
                ],
            }
        ]
    )
    assert '"status_code": 403' in json.dumps(repository_authority, sort_keys=True)
    _assert_browser_sanitizer_precedes_each_upstream(trusted_routes)

    rejected = next(
        route
        for route in routes
        if "match" not in route and '"status_code": 421' in json.dumps(route)
    )
    assert '"status_code": 421' in json.dumps(rejected)


def test_mtls_image_upload_has_a_dedicated_bound_without_widening_other_edges() -> None:
    environments = (
        (
            _adapted_development_caddy(),
            "agents.test.example",
            "enroll.test.example",
        ),
        (
            _adapted_caddy(_environment()),
            "agents.control.test.example",
            "enroll.control.test.example",
        ),
    )
    upload_match = [
        {
            "method": ["PUT"],
            "path": ["/agent/recipe-builds/*/image"],
        }
    ]
    ordinary_match = [{"not": upload_match}]

    for adapted, agent_hostname, enrollment_hostname in environments:
        backend = _server_on_port(adapted, 8443)
        agent_site = next(
            route
            for route in backend["routes"]
            if route.get("match") == [{"host": [agent_hostname]}]
        )
        enrollment_site = next(
            route
            for route in backend["routes"]
            if route.get("match") == [{"host": [enrollment_hostname]}]
        )

        assert _request_body_routes(agent_site) == [
            {"match": ordinary_match, "max_size": 1_000_000},
            {"match": upload_match, "max_size": 16 * 1024**4},
        ]
        assert _request_body_routes(enrollment_site) == [
            {"match": None, "max_size": 1_000_000}
        ]


def test_browser_requests_share_the_ordinary_body_bound() -> None:
    adapted = _adapted_caddy(_environment())
    browser = _server_on_port(adapted, 8080)
    trusted = next(
        route
        for route in _routes_with_handlers(browser["routes"])
        if route.get("match") == [{"host": [_environment()["VONK_CONTROL_HOSTNAME"]]}]
    )
    trusted_routes = trusted["handle"][0]["routes"]

    assert _request_body_routes(trusted_routes) == [
        {"match": None, "max_size": 1_000_000}
    ]


def test_caddy_adapts_three_sni_boundaries_for_admin_enrollment_and_mtls_agents() -> (
    None
):
    environment = _environment()
    rendered_caddy = _rendered("compose.yaml")["services"]["caddy"]
    caddy_environment = rendered_caddy["environment"]
    # The entrypoint derives the other SNI names from this one hostname.
    assert caddy_environment == {
        "VONK_CONTROL_HOSTNAME": environment["VONK_CONTROL_HOSTNAME"]
    }
    adapted = _adapted_caddy(
        caddy_environment | {"VONK_AGENT_PROXY_AUTH": "test-proxy-secret"}
    )
    tailnet_server = _server_on_port(adapted, 8080)
    backend_server = _server_on_port(adapted, 8443)

    def site(host: str) -> dict:
        return next(
            route
            for route in backend_server["routes"]
            if route.get("match") == [{"host": [host]}]
        )

    control_site = next(
        route
        for route in _routes_with_handlers(tailnet_server["routes"])
        if route.get("match")
        == [{"host": [caddy_environment["VONK_CONTROL_HOSTNAME"]]}]
    )
    control_routes = control_site["handle"][0]["routes"]
    denied = next(
        index
        for index, route in enumerate(control_routes)
        if route.get("match") == [{"path": ["/agent/*"]}]
    )
    fallback = next(
        index
        for index, route in enumerate(control_routes)
        if "control-api:8000" in json.dumps(route, sort_keys=True)
    )
    assert denied < fallback

    enrollment_routes = site("enroll.control.test.example")["handle"][0]["routes"]
    enrollment_proxies = [
        route
        for route in enrollment_routes
        if "control-api:8000" in json.dumps(route, sort_keys=True)
    ]
    assert {
        json.dumps(route["match"], sort_keys=True) for route in enrollment_proxies
    } == {
        json.dumps(
            [{"method": ["GET"], "path": ["/agent/bootstrap"]}],
            sort_keys=True,
        ),
        json.dumps(
            [{"method": ["POST"], "path": ["/agent/enroll"]}],
            sort_keys=True,
        ),
    }
    assert any(
        route.get("handle") == [{"handler": "static_response", "status_code": 404}]
        for route in enrollment_routes
    )

    agent_site = site("agents.control.test.example")
    client_auth = next(
        policy["client_authentication"]
        for policy in backend_server["tls_connection_policies"]
        if "agents.control.test.example" in policy.get("match", {}).get("sni", [])
    )
    assert client_auth["mode"] == "require_and_verify"
    assert client_auth["ca"] == {
        "provider": "file",
        "pem_files": ["/run/secrets/agent-client-ca"],
    }
    agent_routes = agent_site["handle"][0]["routes"]
    agent_proxy = next(
        route
        for route in agent_routes
        if "control-api:8000" in json.dumps(route, sort_keys=True)
    )
    agent_handlers = agent_proxy["handle"][0]["routes"][0]["handle"]
    sanitizer_index = next(
        index
        for index, handler in enumerate(agent_handlers)
        if handler.get("handler") == "headers"
    )
    proxy_index = next(
        index
        for index, handler in enumerate(agent_handlers)
        if handler.get("handler") == "reverse_proxy"
    )
    assert sanitizer_index < proxy_index
    assert agent_handlers[sanitizer_index]["request"]["delete"] == ["X-Vonk-Agent-*"]
    request_headers = agent_handlers[proxy_index]["headers"]["request"]
    assert "delete" not in request_headers
    replacements = {key.lower(): value for key, value in request_headers["set"].items()}
    assert replacements == {
        "x-vonk-agent-node": ["{vonk_agent_node}"],
        "x-vonk-agent-serial": ["{http.request.tls.client.serial}"],
        "x-vonk-agent-fingerprint": ["{http.request.tls.client.fingerprint}"],
        "x-vonk-agent-verified": ["1"],
        "x-vonk-agent-proxy-auth": ["test-proxy-secret"],
        "x-vonk-agent-source": ["{http.request.remote.host}"],
    }
    assert any(
        route.get("match")
        == [{"not": [{"path": ["/agent/enroll"]}], "path": ["/agent/*"]}]
        for route in agent_routes
    )
    mappings = []

    def collect_maps(value: object) -> None:
        if isinstance(value, dict):
            if value.get("handler") == "map":
                mappings.append(value)
            for child in value.values():
                collect_maps(child)
        elif isinstance(value, list):
            for child in value:
                collect_maps(child)

    collect_maps(adapted)
    assert mappings == [
        {
            "handler": "map",
            "source": "{http.request.tls.client.subject}",
            "destinations": ["{vonk_agent_node}"],
            "defaults": [""],
            "mappings": [
                {"input_regexp": "^CN=(spk_[0-9a-f]{32})$", "outputs": ["${1}"]}
            ],
        }
    ]


def test_tailnet_and_node_backend_routes_are_on_separate_listeners() -> None:
    environment = _environment()
    adapted = _adapted_caddy(environment)

    tailnet = json.dumps(_server_on_port(adapted, 8080), sort_keys=True)
    backend = json.dumps(_server_on_port(adapted, 8443), sort_keys=True)

    assert "control-api:8000" in tailnet
    assert "litellm:4000" in tailnet
    assert "grafana:3000" in tailnet
    for hostname in (
        "enroll.control.test.example",
        "agents.control.test.example",
        "registry.control.test.example",
    ):
        assert hostname not in tailnet

    assert "enroll.control.test.example" in backend
    assert "agents.control.test.example" in backend
    assert "registry.control.test.example" in backend
    # Lab mode also serves the browser surface on the LAN HTTPS listener.
    # Secure remote browser traffic still enters through the separate tailnet
    # listener above; node-only agent and registry routes remain on 8443.
    assert "control.test.example" in backend
    assert "litellm:4000" not in backend
    assert "grafana:3000" not in backend


def test_caddy_activation_route_is_exposed_only_on_verified_mtls_agent_sni() -> None:
    caddy_environment = _rendered("compose.yaml")["services"]["caddy"]["environment"]
    adapted = _adapted_caddy(
        caddy_environment | {"VONK_AGENT_PROXY_AUTH": "test-proxy-secret"}
    )
    tailnet_server = _server_on_port(adapted, 8080)
    backend_server = _server_on_port(adapted, 8443)
    activation_path = "/agent/renew/activate"

    def site(host: str) -> dict:
        return next(
            route
            for route in backend_server["routes"]
            if route.get("match") == [{"host": [host]}]
        )

    agent_policy = next(
        policy
        for policy in backend_server["tls_connection_policies"]
        if "agents.control.test.example" in policy.get("match", {}).get("sni", [])
    )
    assert agent_policy["client_authentication"]["mode"] == "require_and_verify"

    agent_routes = site("agents.control.test.example")["handle"][0]["routes"]
    agent_proxy = next(
        route
        for route in agent_routes
        if "control-api:8000" in json.dumps(route, sort_keys=True)
    )
    agent_path_pattern = agent_proxy["match"][0]["path"][0]
    assert fnmatchcase(activation_path, agent_path_pattern)

    enrollment_routes = site("enroll.control.test.example")["handle"][0]["routes"]
    enrollment_proxy = next(
        route
        for route in enrollment_routes
        if "control-api:8000" in json.dumps(route, sort_keys=True)
    )
    assert not fnmatchcase(activation_path, enrollment_proxy["match"][0]["path"][0])

    control_site = next(
        route
        for route in _routes_with_handlers(tailnet_server["routes"])
        if route.get("match")
        == [{"host": [caddy_environment["VONK_CONTROL_HOSTNAME"]]}]
    )
    control_routes = control_site["handle"][0]["routes"]
    control_denial = next(
        route
        for route in control_routes
        if fnmatchcase(
            activation_path,
            route.get("match", [{}])[0].get("path", [""])[0],
        )
    )
    assert '"handler": "static_response"' in json.dumps(control_denial, sort_keys=True)
    assert '"status_code": 404' in json.dumps(control_denial, sort_keys=True)


def test_caddy_requires_a_valid_control_hostname_and_proxy_auth(
    tmp_path: Path,
) -> None:
    missing = _environment()
    missing.pop("VONK_CONTROL_HOSTNAME")
    absent = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "deploy/compose/compose.yaml"),
            "config",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        env=missing,
        check=False,
    )
    assert absent.returncode != 0
    assert "VONK_CONTROL_HOSTNAME" in absent.stderr

    valid = {"VONK_CONTROL_HOSTNAME": "control.test.example"}
    short_secret = tmp_path / "agent-proxy-auth"
    short_secret.write_text("short-secret")
    # (environment, secret source, expected stderr). Each case is an
    # independent one-shot container, so they run concurrently.
    cases = [
        ({"VONK_CONTROL_HOSTNAME": "control test.example"}, None, "invalid"),
        ({"VONK_CONTROL_HOSTNAME": "-control.test.example"}, None, "invalid"),
        (valid, None, "proxy authentication secret"),
        (valid, "/dev/null", "proxy authentication secret"),
        (valid, str(short_secret), "base64url-like"),
    ]
    with ThreadPoolExecutor(max_workers=len(cases)) as pool:
        results = list(
            pool.map(lambda case: _entrypoint_result(case[0], case[1]), cases)
        )
    for (environment, secret_source, expected), result in zip(
        cases, results, strict=True
    ):
        assert result.returncode != 0, (environment, secret_source)
        assert expected in result.stderr, (environment, secret_source, result.stderr)


def test_caddy_proxy_auth_is_one_canonical_base64url_like_line(tmp_path: Path) -> None:
    environment = {"VONK_CONTROL_HOSTNAME": "Control.Test.Example."}
    token = "A" * 30 + "_-"
    valid_secret = tmp_path / "valid-agent-proxy-auth"
    valid_secret.write_bytes(token.encode("ascii") + b"\r\n")
    result = _entrypoint_result(
        environment,
        str(valid_secret),
        (
            "/bin/sh",
            "-c",
            (
                'printf "%s %s %s %s %s" "$VONK_AGENT_PROXY_AUTH" '
                '"$VONK_CONTROL_HOSTNAME" "$VONK_AGENT_ENROLL_HOSTNAME" '
                '"$VONK_AGENT_HOSTNAME" "$VONK_REGISTRY_HOSTNAME"'
            ),
        ),
    )
    assert result.returncode == 0, result.stderr
    # One normalized control hostname names every SNI boundary.
    assert result.stdout.split() == [
        token,
        "control.test.example",
        "enroll.control.test.example",
        "agents.control.test.example",
        "registry.control.test.example",
    ]

    invalid_values = (
        b"a" * 31 + b"\n",
        b"a" * 32 + b" ",
        b"a" * 16 + b"!" + b"a" * 16,
        b"a" * 16 + b"\n" + b"a" * 16,
        b"a" * 16 + b"\x00" + b"a" * 16,
    )
    for index, value in enumerate(invalid_values):
        invalid_secret = tmp_path / f"invalid-agent-proxy-auth-{index}"
        invalid_secret.write_bytes(value)
        result = _entrypoint_result(environment, str(invalid_secret))
        assert result.returncode != 0
        assert "base64url-like" in result.stderr


def test_caddy_entrypoint_reads_owner_protected_secrets_with_deployed_capabilities() -> (
    None
):
    # Break caught: cap_drop ALL removed secret-read authority, restarting the
    # installed Caddy container even though its listener uses a high port.
    # A Docker volume preserves real Linux ownership; macOS bind mounts may
    # project their owner as container root and hide this permission failure.
    _require_docker_runtime()
    volume = f"vonk-caddy-secret-regression-{uuid4().hex}"
    subprocess.run(
        ["docker", "volume", "create", volume], check=True, capture_output=True
    )
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{volume}:/secrets",
                "--entrypoint",
                "/bin/sh",
                DEV_CADDY_IMAGE,
                "-c",
                (
                    "touch /secrets/controller-server-certificate /secrets/controller-server-key /secrets/agent-client-ca; "
                    'printf "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\\n" > /secrets/agent-proxy-auth; '
                    "chmod 600 /secrets/agent-proxy-auth; chown 10001:10001 /secrets/agent-proxy-auth"
                ),
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )
        service = _rendered()["services"]["caddy"]
        options = [
            "--read-only",
            "--network",
            "none",
            "-v",
            f"{volume}:/run/secrets:ro",
        ]
        for option, field in (
            ("--cap-drop", "cap_drop"),
            ("--cap-add", "cap_add"),
            ("--security-opt", "security_opt"),
        ):
            for value in service.get(field, []):
                options.extend((option, value))
        result = _entrypoint_result(
            {"VONK_CONTROL_HOSTNAME": "control.test.example"},
            entrypoint_arguments=(
                "/bin/sh",
                "-c",
                'test "${#VONK_AGENT_PROXY_AUTH}" -eq 32',
            ),
            runtime_options=tuple(options),
        )
        assert result.returncode == 0, result.stderr
        # The exact original hardening boundary fails with these same bytes.
        without_read_authority = [
            "--read-only",
            "--network",
            "none",
            "-v",
            f"{volume}:/run/secrets:ro",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
        ]
        refused = _entrypoint_result(
            {"VONK_CONTROL_HOSTNAME": "control.test.example"},
            entrypoint_arguments=("/bin/true",),
            runtime_options=tuple(without_read_authority),
        )
        assert refused.returncode != 0 and "Permission denied" in refused.stderr
    finally:
        subprocess.run(
            ["docker", "volume", "rm", volume], check=True, capture_output=True
        )


def test_rendered_production_boundary_has_only_caddy_public_and_step_ca_private() -> (
    None
):
    rendered = _rendered()
    services = rendered["services"]
    assert {name for name, service in services.items() if service.get("ports")} == {
        "caddy"
    }
    assert set(services["caddy"]["networks"]) == {
        "agent-proxy",
        "hermes-inference",
        "ingress",
        "litellm-edge",
        "registry-edge",
        "tailnet-web-edge",
    }
    assert set(services["control-api"]["networks"]) == {
        "agent-proxy",
        "application",
        "ca",
        "data",
    }
    assert rendered["networks"]["agent-proxy"]["internal"] is True
    assert "step-ca" in services
    assert not services["step-ca"].get("ports")
    assert {secret["source"] for secret in services["caddy"]["secrets"]} >= {
        "agent-client-ca",
        "agent-proxy-auth",
    }
    assert "agent-ca-credential" in {
        secret["source"] for secret in services["control-api"]["secrets"]
    }
    assert services["step-ca"].get("secrets", []) == []
    assert services["step-ca"]["command"][-1] == (
        "/run/vonk-normalized-secrets/step-ca/password"
    )
    assert (
        "step-ca/intermediate-key"
        in (ROOT / "deploy/compose/step-ca/ca.json").read_text()
    )
    assert "root-private" not in json.dumps(services["step-ca"], sort_keys=True).lower()


def test_step_ca_waits_for_api_staged_secrets_without_a_dependency_cycle() -> None:
    rendered = _rendered()
    service = rendered["services"]["step-ca"]
    assert service.get("depends_on", {}).get("control-api") == {
        "condition": "service_healthy",
        "required": True,
    }
    assert "step-ca" not in rendered["services"]["control-api"].get("depends_on", {})
    targets = {volume["target"]: volume for volume in service["volumes"]}
    assert targets["/home/step"]["source"] == "step-ca-data"
    assert targets["/run/vonk-normalized-secrets"]["source"] == (
        "normalized-private-keys"
    )
    assert targets["/run/vonk-normalized-secrets"]["read_only"] is True
    assert "/home/step/db" not in targets
    assert all(volume.get("type") != "bind" for volume in service["volumes"])
    assert "step-ca-config" in {
        secret["source"] for secret in rendered["services"]["control-api"]["secrets"]
    }
    assert "https://step-ca:9000" in service["healthcheck"]["test"]
    assert "https://127.0.0.1:9000" not in service["healthcheck"]["test"]


def test_control_api_has_no_repository_or_git_runtime_mounts() -> None:
    rendered = _rendered()
    api = rendered["services"]["control-api"]
    assert "group_add" not in api
    assert all(
        "/repository" not in json.dumps(volume) for volume in api.get("volumes", [])
    )
    assert "VONK_REPOSITORY_PATH" not in api.get("environment", {})
    assert "VONK_GIT_SIGNING_KEY_FILE" not in api.get("environment", {})


def _stored_file_snippet() -> str:
    text = (ROOT / "deploy/compose/Caddyfile").read_text()
    match = re.search(
        r"^\(stored_file_from_controller\) \{\n.*?^\}\n", text, re.DOTALL | re.MULTILINE
    )
    assert match is not None
    return match.group(0)


@pytest.mark.slow(30)
def test_caddy_serves_the_file_the_controller_names_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The real snippet in front of a stub Controller and a real object store.

    Catches: a client-visible internal header, Caddy's own ETag replacing the
    Controller's, a path outside the two content-addressed layouts, and ranges
    that are not honoured.
    """
    import http.client
    import time

    _require_docker_runtime()
    digest = "ab" + "0" * 62
    payload = bytes(range(256)) * 16
    objects = tmp_path / "state"
    (objects / "model-cache/objects/ab").mkdir(parents=True)
    (objects / "model-cache/objects/ab" / digest).write_bytes(payload)
    (objects / "secret").write_bytes(b"controller state")
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(
        "{\n\tadmin off\n\tauto_https off\n}\n"
        + _stored_file_snippet()
        + ":8080 {\n\treverse_proxy 127.0.0.1:9000 {\n\t\timport stored_file_from_controller\n\t}\n}\n"
        # Stub Controller: it names whatever the request's ?file= asks for.
        + ":9000 {\n\t@denied query denied=1\n\thandle @denied {\n\t\trespond 403\n\t}\n"
        '\thandle {\n\t\theader X-Vonk-File {query.file}\n\t\theader ETag "\\"sha256:controller\\""\n'
        "\t\theader Cache-Control no-store\n\t\trespond 200\n\t}\n}\n"
    )
    container = f"vonk-stored-file-{os.getpid()}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container,
            "-p",
            "127.0.0.1::8080",
            "-v",
            f"{caddyfile}:/etc/caddy/Caddyfile:ro",
            "-v",
            f"{objects}:/srv/state:ro",
            DEV_CADDY_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = int(
            subprocess.run(
                ["docker", "port", container, "8080/tcp"],
                check=True,
                capture_output=True,
                text=True,
            )
            .stdout.splitlines()[0]
            .rsplit(":", 1)[1]
        )

        def get(query: str, **headers: str) -> http.client.HTTPResponse:
            for _ in range(50):
                try:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=5
                    )
                    connection.request("GET", "/x?" + query, headers=headers)
                    response = connection.getresponse()
                    response.body = response.read()  # type: ignore[attr-defined]
                    return response
                except (ConnectionError, http.client.RemoteDisconnected):
                    time.sleep(0.1)
            raise AssertionError("caddy did not start")

        stored = f"file=model-cache/objects/ab/{digest}"
        ranged = get(stored, Range="bytes=10-19", **{"If-Range": '"sha256:controller"'})
        assert ranged.status == 206
        assert ranged.body == payload[10:20]
        assert ranged.getheader("Content-Range") == f"bytes 10-19/{len(payload)}"
        # The Controller's identity for the object, not the file server's.
        assert ranged.getheader("ETag") == '"sha256:controller"'
        assert ranged.getheader("X-Vonk-File") is None
        assert get(stored).body == payload
        for refused in (
            "file=secret",
            "file=model-cache/objects/ab/../../../secret",
            f"file=%2E%2E/{digest}",
            f"file=model-cache/objects/cd/{digest}",
        ):
            response = get(refused)
            assert response.status == 404, refused
            assert response.getheader("X-Vonk-File") is None
        assert get("denied=1").status == 403
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container], capture_output=True, check=False
        )
