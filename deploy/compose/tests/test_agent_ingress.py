import json
import os
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEV_CADDYFILE = (
    ROOT / "control/src/vonk_control/resources/dev/Caddyfile"
)
DEV_CADDY_IMAGE = (
    "caddy:2.11.4@sha256:"
    "844f60b64e4724a5aa8245e019dace0d3f199f7433ce6c57676cb30a920dbad9"
)


def _environment() -> dict[str, str]:
    return os.environ | {
        "POSTGRES_IMAGE": "postgres:17@sha256:" + "a" * 64,
        "CADDY_IMAGE": "caddy:2@sha256:" + "b" * 64,
        "REGISTRY_IMAGE": "registry:3@sha256:" + "9" * 64,
        "CONTROL_API_IMAGE": "example/control-api:1@sha256:" + "c" * 64,
        "CONTROL_WORKER_IMAGE": "example/control-worker:1@sha256:" + "8" * 64,
        "HERMES_AGENT_IMAGE": "example/hermes:1@sha256:" + "7" * 64,
        "LITELLM_IMAGE": "example/litellm:1@sha256:" + "d" * 64,
        "PROMETHEUS_IMAGE": "prom/prometheus:1@sha256:" + "e" * 64,
        "GRAFANA_IMAGE": "grafana/grafana:1@sha256:" + "f" * 64,
        "STEP_CA_IMAGE": "smallstep/step-ca:0.30.2@sha256:" + "1" * 64,
        "TAILSCALE_IMAGE": "tailscale/tailscale:v1.98.8@sha256:d54b2e6a9c09f0e5ec52e82b9ad4af3d446b54a7c08075e92f11c39dd410105f",
        "REPOSITORY_PATH": "/srv/vonk-forge/repository",
        "DATABASE_URL_FILE": "/dev/null",
        "POSTGRES_PASSWORD_FILE": "/dev/null",
        "TOKEN_SIGNING_KEY_FILE": "/dev/null",
        "METRICS_TOKEN_FILE": "/dev/null",
        "GIT_SIGNING_KEY_FILE": "/dev/null",
        "WORKER_API_TOKEN_FILE": "/dev/null",
        "AGENT_UPDATE_AUTHORITY_KEY_FILE": "/dev/null",
        "ADMIN_GRANT_PRIVATE_KEY_FILE": "/dev/null",
        "PACKAGE_HELPER_GRANT_PRIVATE_KEY_FILE": "/dev/null",
        "PACKAGE_HELPER_RECEIPT_PRIVATE_KEY_FILE": "/dev/null",
        "HOST_RUNTIME_GRANT_PRIVATE_KEY_FILE": "/dev/null",
        "WORKLOAD_RELEASES_KEY_FILE": "/dev/null",
        "WORKLOAD_SNAPSHOT_KEY_FILE": "/dev/null",
        "WORKLOAD_TIMESTAMP_KEY_FILE": "/dev/null",
        "ADMIN_GRANT_PUBLIC_KEY_FILE": "/dev/null",
        "AGENT_TUF_BOOTSTRAP_ROOT_FILE": "/dev/null",
        "CONTROL_IDENTITY_PATH": "/srv/vonk-forge/control-identity",
        "VONK_PLATFORM_VERSION": "1.0.0",
        "VONK_PLATFORM_RELEASE_DIGEST": "sha256:" + "2" * 64,
        "VONK_PLATFORM_BUILD_DIGEST": "sha256:" + "3" * 64,
        "VONK_CONTROL_GENERATION_ID": "gen-" + "4" * 24,
        "VONK_DATABASE_REVISION": "0012_control_process_heartbeats",
        "VONK_CONTROL_START_NONCE": "5" * 64,
        "GRAFANA_ADMIN_PASSWORD_FILE": "/dev/null",
        "LITELLM_MASTER_KEY_FILE": "/dev/null",
        "LITELLM_UPSTREAM_KEY_FILE": "/dev/null",
        "LITELLM_DATABASE_URL_FILE": "/dev/null",
        "AGENT_CLIENT_CA_FILE": "/dev/null",
        "CONTROLLER_CA_FILE": "/dev/null",
        "AGENT_INTERMEDIATE_CERTIFICATE_FILE": "/dev/null",
        "AGENT_PROXY_AUTH_FILE": "/dev/null",
        "AGENT_CA_CREDENTIAL_FILE": "/dev/null",
        "AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE": "/dev/null",
        "AGENT_CA_PROVISIONER_KID": "test-provisioner-kid",
        "STEP_CA_CONFIG_FILE": "/dev/null",
        "STEP_CA_INTERMEDIATE_KEY_FILE": "/dev/null",
        "STEP_CA_PASSWORD_FILE": "/dev/null",
        "STEP_CA_ROOT_CERTIFICATE_FILE": "/dev/null",
        "AGENT_INTERMEDIATE_KEY_FILE": "/dev/null",
        "VONK_CONTROL_HOSTNAME": "control.test.example",
        "VONK_AGENT_ENROLL_HOSTNAME": "enroll.test.example",
        "VONK_AGENT_HOSTNAME": "agents.test.example",
        "VONK_REGISTRY_HOSTNAME": "registry.test.example",
        "VONK_AGENT_PROXY_AUTH": "test-proxy-secret",
        "VONK_MANAGEMENT_CIDRS": "10.0.0.0/24",
        "VONK_DIRECT_FABRIC_CIDRS": "192.168.100.0/24,192.168.101.0/24",
        "NAS_LAN_IP": "10.0.0.2",
        "VONK_BACKEND_PORT": "8443",
        "TAILSCALE_OAUTH_CLIENT_ID_FILE": "/dev/null",
        "TAILSCALE_OAUTH_CLIENT_SECRET_FILE": "/dev/null",
        "HERMES_UID": "1100",
        "HERMES_GID": "1100",
        "HERMES_DATA_ROOT": "/srv/vonk-forge/hermes",
        "HERMES_API_KEY_FILE": "/dev/null",
        "HERMES_DASHBOARD_ORIGIN": "https://hermes.test.example",
    }


def _rendered(*files: str, environment: dict[str, str] | None = None) -> dict:
    command = ["docker", "compose"]
    for file in files or ("compose.yaml", "compose.step-ca.yaml"):
        command.extend(("-f", str(ROOT / "deploy/compose" / file)))
    command.extend(("config", "--format", "json"))
    result = subprocess.run(command, check=True, capture_output=True, text=True, env=environment or _environment())
    return json.loads(result.stdout)


def _adapted_caddy(environment: dict[str, str]) -> dict:
    result = subprocess.run(
        [
            "docker", "run", "--rm", "-i",
            "-e", f"VONK_CONTROL_HOSTNAME={environment['VONK_CONTROL_HOSTNAME']}",
            "-e", f"VONK_AGENT_ENROLL_HOSTNAME={environment['VONK_AGENT_ENROLL_HOSTNAME']}",
            "-e", f"VONK_AGENT_HOSTNAME={environment['VONK_AGENT_HOSTNAME']}",
            "-e", f"VONK_REGISTRY_HOSTNAME={environment['VONK_REGISTRY_HOSTNAME']}",
            "-e", f"VONK_BACKEND_PORT={environment.get('VONK_BACKEND_PORT', '8443')}",
            "-e", "VONK_AGENT_PROXY_AUTH=test-proxy-secret",
            "caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb",
            "caddy", "adapt", "--config", "-", "--adapter", "caddyfile",
        ],
        check=True,
        capture_output=True,
        text=True,
        input=(ROOT / "deploy/compose/Caddyfile").read_text(),
    )
    return json.loads(result.stdout)


def _server_on_port(adapted: dict, port: int) -> dict:
    suffix = f":{port}"
    return next(
        server
        for server in adapted["apps"]["http"]["servers"].values()
        if any(
            str(listener).endswith(suffix)
            for listener in server.get("listen", [])
        )
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
) -> subprocess.CompletedProcess[str]:
    environment = environment | {
        "VONK_REGISTRY_HOSTNAME": environment.get(
            "VONK_REGISTRY_HOSTNAME", "registry.test.example"
        ),
        "VONK_BACKEND_PORT": environment.get("VONK_BACKEND_PORT", "8443"),
    }
    command = ["docker", "run", "--rm"]
    for name, value in environment.items():
        command.extend(("-e", f"{name}={value}"))
    command.extend((
        "-v", f"{ROOT / 'deploy/compose/caddy/entrypoint.sh'}:/usr/local/bin/vonk-caddy-entrypoint:ro",
    ))
    if secret_source is not None:
        command.extend(("-v", f"{secret_source}:/run/secrets/agent-proxy-auth:ro"))
    command.extend((
        "caddy:2.10.2@sha256:c3d7ee5d2b11f9dc54f947f68a734c84e9c9666c92c88a7f30b9cba5da182adb",
        "/bin/sh", "/usr/local/bin/vonk-caddy-entrypoint",
    ))
    command.extend(entrypoint_arguments)
    return subprocess.run(
        command, capture_output=True, text=True, timeout=10, check=False
    )


def _settings_result(rendered: dict, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("VONK_")
    }
    control_environment = rendered["services"]["control-api"]["environment"].copy()
    secret_values = {
        "VONK_DATABASE_URL_FILE": "postgresql://control:pw@postgres/control\n",
        "VONK_TOKEN_SIGNING_KEY_FILE": "t" * 32 + "\n",
        "VONK_METRICS_TOKEN_FILE": "m" * 16 + "\n",
        "VONK_GIT_SIGNING_KEY_FILE": "test-git-key\n",
        "VONK_AGENT_CLIENT_CA_FILE": "test-client-ca\n",
        "VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "test-intermediate-certificate\n",
        "VONK_AGENT_CA_CREDENTIAL_FILE": "test-provider-credential\n",
        "VONK_AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE": "test-provider-public-jwk\n",
        "VONK_AGENT_CA_ROOT_FILE": "test-root-certificate\n",
        "VONK_CONTROLLER_CA_FILE": "test-controller-ca\n",
        "VONK_AGENT_INTERMEDIATE_KEY_FILE": "test-builtin-key\n",
        "VONK_AGENT_PROXY_AUTH_FILE": "A" * 30 + "_-\r\n",
        "VONK_WORKER_API_TOKEN_FILE": "W" * 32 + "\n",
        "VONK_MANAGEMENT_CIDRS_FILE": "10.0.0.0/24\n",
        "VONK_ADMIN_GRANT_PRIVATE_KEY_FILE": "test-admin-grant-private-key\n",
        "VONK_PACKAGE_HELPER_GRANT_PRIVATE_KEY_FILE": "test-package-grant-key\n",
        "VONK_PACKAGE_HELPER_RECEIPT_PRIVATE_KEY_FILE": "test-package-receipt-key\n",
        "VONK_HOST_RUNTIME_GRANT_PRIVATE_KEY_FILE": "test-host-runtime-key\n",
    }
    for name, value in tuple(control_environment.items()):
        if name not in secret_values:
            continue
        secret = tmp_path / name.lower()
        secret.write_text(secret_values[name])
        control_environment[name] = str(secret)
    control_environment.setdefault("VONK_AGENT_CA_PROVISIONER_NAME", "vonk-forge-agent")
    control_environment.setdefault("VONK_AGENT_CA_PROVISIONER_KID", "test-provisioner-kid")
    environment.update({name: str(value) for name, value in control_environment.items()})
    return subprocess.run(
        [
            "uv", "run", "--project", str(ROOT / "control"), "python", "-c",
            (
                "from vonk_control.settings import Settings; "
                "settings = Settings.from_env_and_secrets(); "
                "print(settings.agent_ca_provider); "
                "print(settings.agent_proxy_auth.decode('ascii')); "
                "print(settings.management_cidrs); "
                "print(settings.direct_fabric_cidrs)"
            ),
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def test_development_image_compose_enables_complete_builtin_agent_settings(
    tmp_path: Path,
) -> None:
    rendered = _rendered("compose.dev.images.yaml")
    services = rendered["services"]
    api = services["control-api"]
    caddy = services["caddy"]

    result = _settings_result(rendered, tmp_path / "settings")
    assert result.returncode == 0, result.stderr
    provider, proxy_auth, management, direct_fabric = result.stdout.splitlines()
    assert provider == "builtin"
    assert proxy_auth == "A" * 30 + "_-"
    assert management == "10.0.0.0/24"
    assert direct_fabric == ""

    assert api["environment"]["VONK_AGENT_RUNTIME"] == "enabled"
    assert api["environment"]["VONK_AGENT_BUILTIN_CA_BOOTSTRAP"] == "1"
    assert api["environment"]["VONK_MANAGEMENT_CIDRS_FILE"] == (
        "/run/secrets/management-cidrs"
    )
    assert set(caddy["networks"]) == {
        "application",
        "ingress",
        "litellm-edge",
        "tailnet-web-edge",
    }
    assert set(api["networks"]) == {"application", "data", "ingress"}
    assert set(services["litellm"]["networks"]) == {
        "cluster-egress",
        "litellm-data",
        "litellm-edge",
    }
    assert services["litellm"].get("ports") in (None, [])
    assert caddy["depends_on"]["control-api"] == {
        "condition": "service_healthy",
        "required": True,
    }


def test_agent_bootstrap_uses_distinct_https_origins_and_public_ca_only() -> None:
    rendered = _rendered()
    api = rendered["services"]["control-api"]
    environment = api["environment"]
    api_secrets = {secret["source"] for secret in api["secrets"]}

    assert environment["VONK_AGENT_CONTROLLER_ORIGIN"] == (
        "https://agents.test.example:8443"
    )
    assert environment["VONK_AGENT_ENROLLMENT_ORIGIN"] == (
        "https://enroll.test.example:8443"
    )
    assert environment["VONK_CONTROLLER_CA_FILE"] == "/run/secrets/controller-ca"
    assert "controller-ca" in api_secrets
    assert "controller-server-key" not in api_secrets
    assert "agent-intermediate-key" not in api_secrets


def test_development_caddy_health_listener_is_exact_and_loopback_only() -> None:
    adapted = _adapted_development_caddy()
    health = _server_on_port(adapted, 2019)

    assert health["listen"] == ["127.0.0.1:2019"]
    assert health["routes"] == [
        {
            "match": [{"host": ["127.0.0.1"]}],
            "handle": [
                {
                    "handler": "subroute",
                    "routes": [
                        {
                            "handle": [
                                {"handler": "static_response", "status_code": 200}
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
    assert "127.0.0.1:2019" in listeners
    assert ":2019" not in listeners
    assert "0.0.0.0:2019" not in listeners
    assert "[::]:2019" not in listeners


def test_development_browser_edge_accepts_only_the_canonical_tailscale_service_host() -> None:
    adapted = _adapted_development_caddy()
    browser = _server_on_port(adapted, 8080)

    assert browser["listen"] == [":8080"]
    routes = _routes_with_handlers(browser["routes"])
    trusted = next(
        route
        for route in routes
        if route.get("match")
        == [{"host": ["vonk-forge.tailnet.test.ts.net"]}]
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
        ("/agent/v1/*", 404),
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


def test_production_browser_edge_accepts_only_control_hostname_and_fails_closed() -> None:
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
    for path in ("/agent/v1/*", "/internal/*"):
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
            "agents.test.example",
            "enroll.test.example",
        ),
    )
    upload_match = [
        {
            "method": ["PUT"],
            "path": ["/agent/v1/recipe-builds/*/image"],
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


def test_caddy_adapts_three_sni_boundaries_for_admin_enrollment_and_mtls_agents() -> None:
    environment = _environment()
    rendered_caddy = _rendered("compose.yaml")["services"]["caddy"]
    caddy_environment = rendered_caddy["environment"]
    assert {name: caddy_environment[name] for name in (
        "VONK_CONTROL_HOSTNAME", "VONK_AGENT_ENROLL_HOSTNAME", "VONK_AGENT_HOSTNAME",
    )} == {name: environment[name] for name in (
        "VONK_CONTROL_HOSTNAME", "VONK_AGENT_ENROLL_HOSTNAME", "VONK_AGENT_HOSTNAME",
    )}
    adapted = _adapted_caddy(caddy_environment | {"VONK_AGENT_PROXY_AUTH": "test-proxy-secret"})
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
        if route.get("match") == [{"host": [caddy_environment["VONK_CONTROL_HOSTNAME"]]}]
    )
    control_routes = control_site["handle"][0]["routes"]
    denied = next(
        index for index, route in enumerate(control_routes)
        if route.get("match") == [{"path": ["/agent/v1/*"]}]
    )
    fallback = next(
        index for index, route in enumerate(control_routes)
        if "control-api:8000" in json.dumps(route, sort_keys=True)
    )
    assert denied < fallback

    enrollment_routes = site("enroll.test.example")["handle"][0]["routes"]
    enrollment_proxy = next(route for route in enrollment_routes if "control-api:8000" in json.dumps(route, sort_keys=True))
    assert enrollment_proxy["match"] == [{"path": ["/agent/v1/enroll"]}]
    assert any(route.get("match") == [{"not": [{"path": ["/agent/v1/enroll"]}]}] for route in enrollment_routes)

    agent_site = site("agents.test.example")
    client_auth = next(
        policy["client_authentication"]
        for policy in backend_server["tls_connection_policies"]
        if "agents.test.example" in policy.get("match", {}).get("sni", [])
    )
    assert client_auth["mode"] == "require_and_verify"
    assert client_auth["ca"] == {"provider": "file", "pem_files": ["/run/secrets/agent-client-ca"]}
    agent_routes = agent_site["handle"][0]["routes"]
    agent_proxy = next(route for route in agent_routes if "control-api:8000" in json.dumps(route, sort_keys=True))
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
    assert agent_handlers[sanitizer_index]["request"]["delete"] == [
        "X-Vonk-Agent-*"
    ]
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
    assert any(route.get("match") == [{"not": [{"path": ["/agent/v1/enroll"]}], "path": ["/agent/v1/*"]}] for route in agent_routes)
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
    assert mappings == [{
        "handler": "map", "source": "{http.request.tls.client.subject}",
        "destinations": ["{vonk_agent_node}"], "defaults": [""],
        "mappings": [{"input_regexp": "^CN=(spk_[0-9a-f]{32})$", "outputs": ["${1}"]}],
    }]


def test_tailnet_and_node_backend_routes_are_on_separate_listeners() -> None:
    environment = _environment()
    adapted = _adapted_caddy(environment)

    tailnet = json.dumps(_server_on_port(adapted, 8080), sort_keys=True)
    backend = json.dumps(_server_on_port(adapted, 8443), sort_keys=True)

    assert "control-api:8000" in tailnet
    assert "litellm:4000" in tailnet
    assert "grafana:3000" in tailnet
    for hostname in (
        "enroll.test.example",
        "agents.test.example",
        "registry.test.example",
    ):
        assert hostname not in tailnet

    assert "enroll.test.example" in backend
    assert "agents.test.example" in backend
    assert "registry.test.example" in backend
    assert "control.test.example" not in backend
    assert "litellm:4000" not in backend
    assert "grafana:3000" not in backend


def test_caddy_activation_route_is_exposed_only_on_verified_mtls_agent_sni() -> None:
    caddy_environment = _rendered("compose.yaml")["services"]["caddy"][
        "environment"
    ]
    adapted = _adapted_caddy(
        caddy_environment | {"VONK_AGENT_PROXY_AUTH": "test-proxy-secret"}
    )
    tailnet_server = _server_on_port(adapted, 8080)
    backend_server = _server_on_port(adapted, 8443)
    activation_path = "/agent/v1/renew/activate"

    def site(host: str) -> dict:
        return next(
            route
            for route in backend_server["routes"]
            if route.get("match") == [{"host": [host]}]
        )

    agent_policy = next(
        policy
        for policy in backend_server["tls_connection_policies"]
        if "agents.test.example" in policy.get("match", {}).get("sni", [])
    )
    assert agent_policy["client_authentication"]["mode"] == "require_and_verify"

    agent_routes = site("agents.test.example")["handle"][0]["routes"]
    agent_proxy = next(
        route
        for route in agent_routes
        if "control-api:8000" in json.dumps(route, sort_keys=True)
    )
    agent_path_pattern = agent_proxy["match"][0]["path"][0]
    assert fnmatchcase(activation_path, agent_path_pattern)

    enrollment_routes = site("enroll.test.example")["handle"][0]["routes"]
    enrollment_proxy = next(
        route
        for route in enrollment_routes
        if "control-api:8000" in json.dumps(route, sort_keys=True)
    )
    assert not fnmatchcase(activation_path, enrollment_proxy["match"][0]["path"][0])

    control_site = next(
        route
        for route in _routes_with_handlers(tailnet_server["routes"])
        if route.get("match") == [{"host": [caddy_environment["VONK_CONTROL_HOSTNAME"]]}]
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
    assert '"handler": "static_response"' in json.dumps(
        control_denial, sort_keys=True
    )
    assert '"status_code": 404' in json.dumps(control_denial, sort_keys=True)


def test_caddy_compose_requires_distinct_sni_hostnames_before_startup(tmp_path: Path) -> None:
    missing = _environment()
    missing.pop("VONK_AGENT_HOSTNAME")
    command = ["docker", "compose", "-f", str(ROOT / "deploy/compose/compose.yaml"), "config", "--quiet"]
    absent = subprocess.run(
        command, capture_output=True, text=True, env=missing, check=False
    )
    assert absent.returncode != 0
    assert "VONK_AGENT_HOSTNAME" in absent.stderr

    for duplicate in (
        {"VONK_CONTROL_HOSTNAME": "same.test.example", "VONK_AGENT_ENROLL_HOSTNAME": "same.test.example", "VONK_AGENT_HOSTNAME": "agents.test.example"},
        {"VONK_CONTROL_HOSTNAME": "same.test.example", "VONK_AGENT_ENROLL_HOSTNAME": "enroll.test.example", "VONK_AGENT_HOSTNAME": "same.test.example"},
        {"VONK_CONTROL_HOSTNAME": "control.test.example", "VONK_AGENT_ENROLL_HOSTNAME": "same.test.example", "VONK_AGENT_HOSTNAME": "same.test.example"},
    ):
        result = _entrypoint_result(duplicate)
        assert result.returncode != 0
        assert "must be distinct" in result.stderr

    for equivalent in (
        {"VONK_CONTROL_HOSTNAME": "CONTROL.test.example", "VONK_AGENT_ENROLL_HOSTNAME": "control.test.example.", "VONK_AGENT_HOSTNAME": "agents.test.example"},
        {"VONK_CONTROL_HOSTNAME": "control.test.example", "VONK_AGENT_ENROLL_HOSTNAME": "ENROLL.test.example", "VONK_AGENT_HOSTNAME": "enroll.test.example."},
    ):
        result = _entrypoint_result(equivalent)
        assert result.returncode != 0
        assert "must be distinct" in result.stderr

    malformed = _entrypoint_result({
        "VONK_CONTROL_HOSTNAME": "control test.example",
        "VONK_AGENT_ENROLL_HOSTNAME": "enroll.test.example",
        "VONK_AGENT_HOSTNAME": "agents.test.example",
    })
    assert malformed.returncode != 0
    assert "invalid" in malformed.stderr

    valid = {"VONK_CONTROL_HOSTNAME": "control.test.example", "VONK_AGENT_ENROLL_HOSTNAME": "enroll.test.example", "VONK_AGENT_HOSTNAME": "agents.test.example"}
    for result in (_entrypoint_result(valid), _entrypoint_result(valid, "/dev/null")):
        assert result.returncode != 0
        assert "proxy authentication secret" in result.stderr

    short_secret = tmp_path / "agent-proxy-auth"
    short_secret.write_text("short-secret")
    result = _entrypoint_result(valid, str(short_secret))
    assert result.returncode != 0
    assert "base64url-like" in result.stderr


def test_caddy_proxy_auth_is_one_canonical_base64url_like_line(tmp_path: Path) -> None:
    environment = {
        "VONK_CONTROL_HOSTNAME": "control.test.example",
        "VONK_AGENT_ENROLL_HOSTNAME": "enroll.test.example",
        "VONK_AGENT_HOSTNAME": "agents.test.example",
    }
    token = "A" * 30 + "_-"
    valid_secret = tmp_path / "valid-agent-proxy-auth"
    valid_secret.write_bytes(token.encode("ascii") + b"\r\n")
    result = _entrypoint_result(
        environment,
        str(valid_secret),
        ("/bin/sh", "-c", 'printf "%s" "$VONK_AGENT_PROXY_AUTH"'),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == token

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


def test_rendered_production_boundary_has_only_caddy_public_and_step_ca_private() -> None:
    rendered = _rendered()
    services = rendered["services"]
    assert {name for name, service in services.items() if service.get("ports")} == {"caddy"}
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
        "worker-authority",
    }
    assert rendered["networks"]["agent-proxy"]["internal"] is True
    assert "step-ca" in services
    assert not services["step-ca"].get("ports")
    assert {secret["source"] for secret in services["caddy"]["secrets"]} >= {"agent-client-ca", "agent-proxy-auth"}
    assert {secret["source"] for secret in services["control-api"]["secrets"]} >= {
        "agent-client-ca", "agent-intermediate-certificate", "agent-ca-credential", "agent-proxy-auth",
    }
    assert "agent-intermediate-key" not in {secret["source"] for secret in services["control-api"]["secrets"]}
    assert "root-private" not in json.dumps(services["step-ca"], sort_keys=True).lower()


def test_builtin_ca_override_is_explicit_and_only_it_mounts_the_builtin_signing_key() -> None:
    production = _rendered()
    builtin = _rendered("compose.yaml", "compose.builtin-ca.yaml")
    production_secrets = {secret["source"] for secret in production["services"]["control-api"]["secrets"]}
    builtin_secrets = {secret["source"] for secret in builtin["services"]["control-api"]["secrets"]}
    assert "agent-intermediate-key" not in production_secrets
    assert "agent-intermediate-key" in builtin_secrets
    assert "agent-ca-credential" not in builtin_secrets
    assert builtin["services"]["control-api"]["environment"]["VONK_AGENT_CA_PROVIDER"] == "builtin"
    assert builtin["services"]["control-api"]["environment"]["VONK_AGENT_BUILTIN_CA_BOOTSTRAP"] == "1"
    assert "VONK_AGENT_CA_CREDENTIAL_FILE" not in builtin["services"]["control-api"]["environment"]


def test_provider_overlays_require_only_their_own_secrets() -> None:
    base = _rendered("compose.yaml")
    assert "step-ca" not in base["services"]
    assert "VONK_AGENT_CA_PROVIDER" not in base["services"]["control-api"]["environment"]

    builtin_environment = _environment()
    for name in ("AGENT_CA_CREDENTIAL_FILE", "STEP_CA_ROOT_CERTIFICATE_FILE", "STEP_CA_INTERMEDIATE_KEY_FILE", "STEP_CA_PASSWORD_FILE"):
        builtin_environment.pop(name)
    builtin = _rendered("compose.yaml", "compose.builtin-ca.yaml", environment=builtin_environment)
    assert "agent-ca-credential" not in {secret["source"] for secret in builtin["services"]["control-api"]["secrets"]}

    missing_step_secret = _environment()
    missing_step_secret.pop("STEP_CA_PASSWORD_FILE")
    command = ["docker", "compose", "-f", str(ROOT / "deploy/compose/compose.yaml"), "-f", str(ROOT / "deploy/compose/compose.step-ca.yaml"), "config", "--quiet"]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=missing_step_secret,
        check=False,
    )
    assert result.returncode != 0
    assert "STEP_CA_PASSWORD_FILE" in result.stderr


def test_provider_overlays_are_mutually_exclusive_at_application_startup(tmp_path: Path) -> None:
    for overlays in (
        ("compose.step-ca.yaml", "compose.builtin-ca.yaml"),
        ("compose.builtin-ca.yaml", "compose.step-ca.yaml"),
    ):
        rendered = _rendered("compose.yaml", *overlays)
        result = _settings_result(rendered, tmp_path / overlays[0])
        assert result.returncode != 0
        assert "CA provider settings cannot be combined" in result.stderr


def test_each_provider_overlay_passes_application_settings_guard(tmp_path: Path) -> None:
    token = "A" * 30 + "_-"
    for overlay, provider in (
        ("compose.step-ca.yaml", "step-ca"),
        ("compose.builtin-ca.yaml", "builtin"),
    ):
        rendered = _rendered("compose.yaml", overlay)
        environment = rendered["services"]["control-api"]["environment"]
        assert environment["VONK_DEPLOYMENT_MODE"] == "production"
        assert environment["VONK_AGENT_RUNTIME"] == "enabled"
        result = _settings_result(rendered, tmp_path / provider)
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            provider,
            token,
            "10.0.0.0/24",
            "192.168.100.0/24,192.168.101.0/24",
        ]
