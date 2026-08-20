import json
import os
import subprocess
from pathlib import Path


def _rendered() -> dict:
    root = Path(__file__).resolve().parents[3]
    env = os.environ | {
        "POSTGRES_IMAGE": "postgres:17@sha256:" + "a" * 64,
        "CADDY_IMAGE": "caddy:2@sha256:" + "b" * 64,
        "REGISTRY_IMAGE": "registry:3@sha256:" + "9" * 64,
        "CONTROL_API_IMAGE": "example/control-api:1@sha256:" + "c" * 64,
        "CONTROL_WORKER_IMAGE": "example/control-worker:1@sha256:" + "8" * 64,
        "HERMES_AGENT_IMAGE": "example/hermes:1@sha256:" + "7" * 64,
        "LITELLM_IMAGE": "example/litellm:1@sha256:" + "d" * 64,
        "PROMETHEUS_IMAGE": "prom/prometheus:1@sha256:" + "e" * 64,
        "GRAFANA_IMAGE": "grafana/grafana:1@sha256:" + "f" * 64,
        "DATABASE_URL_FILE": "/dev/null",
        "ADMIN_PASSWORD_FILE": "/dev/null",
        "POSTGRES_PASSWORD_FILE": "/dev/null",
        "TOKEN_SIGNING_KEY_FILE": "/dev/null",
        "METRICS_TOKEN_FILE": "/dev/null",
        "WORKER_API_TOKEN_FILE": "/dev/null",
        "PACKAGE_HELPER_GRANT_PRIVATE_KEY_FILE": "/dev/null",
        "PACKAGE_HELPER_RECEIPT_PRIVATE_KEY_FILE": "/dev/null",
        "HOST_RUNTIME_GRANT_PRIVATE_KEY_FILE": "/dev/null",
        "GRAFANA_ADMIN_PASSWORD_FILE": "/dev/null",
        "LITELLM_MASTER_KEY_FILE": "/dev/null",
        "LITELLM_UPSTREAM_KEY_FILE": "/dev/null",
        "LITELLM_DATABASE_URL_FILE": "/dev/null",
        "LITELLM_DATABASE_PASSWORD_FILE": "/dev/null",
        "STEP_CA_IMAGE": "smallstep/step-ca:0.30.2@sha256:" + "1" * 64,
        "TAILSCALE_IMAGE": "tailscale/tailscale:v1.98.8@sha256:d54b2e6a9c09f0e5ec52e82b9ad4af3d446b54a7c08075e92f11c39dd410105f",
        "AGENT_CLIENT_CA_FILE": "/dev/null",
        "AGENT_INTERMEDIATE_CERTIFICATE_FILE": "/dev/null",
        "CONTROLLER_CA_FILE": "/dev/null",
        "CONTROLLER_SERVER_CERTIFICATE_FILE": "/dev/null",
        "CONTROLLER_SERVER_KEY_FILE": "/dev/null",
        "AGENT_PROXY_AUTH_FILE": "/dev/null",
        "AGENT_CA_CREDENTIAL_FILE": "/dev/null",
        "AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE": "/dev/null",
        "AGENT_CA_PROVISIONER_KID": "test-provisioner-kid",
        "STEP_CA_CONFIG_FILE": "/dev/null",
        "STEP_CA_ROOT_CERTIFICATE_FILE": "/dev/null",
        "STEP_CA_INTERMEDIATE_KEY_FILE": "/dev/null",
        "STEP_CA_PASSWORD_FILE": "/dev/null",
        "VONK_CONTROL_HOSTNAME": "control.test.example",
        "VONK_AGENT_ENROLL_HOSTNAME": "enroll.test.example",
        "VONK_AGENT_HOSTNAME": "agents.test.example",
        "VONK_REGISTRY_HOSTNAME": "registry.test.example",
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
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(root / "deploy/compose/compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return json.loads(result.stdout)


def test_only_caddy_publishes_ports_and_images_are_digest_pinned() -> None:
    rendered = _rendered()
    published = {
        name for name, service in rendered["services"].items() if service.get("ports")
    }
    assert published == {"caddy"}
    assert all(
        "@sha256:" in service["image"] or service.get("build")
        for service in rendered["services"].values()
    )


def test_caddy_publishes_only_reserved_nas_backend_listener() -> None:
    caddy = _rendered()["services"]["caddy"]

    assert caddy["ports"] == [
        {
            "mode": "ingress",
            "target": 8443,
            "published": "8443",
            "protocol": "tcp",
            "host_ip": "10.0.0.2",
        }
    ]
    assert caddy["environment"]["VONK_BACKEND_PORT"] == "8443"


def test_litellm_has_no_network_path_from_control_services() -> None:
    rendered = _rendered()
    services = rendered["services"]
    assert set(services["postgres"]["networks"]) == {"data", "litellm-data"}
    assert set(services["caddy"]["networks"]) == {
        "agent-proxy",
        "hermes-inference",
        "ingress",
        "litellm-edge",
        "registry-edge",
        "tailnet-web-edge",
    }
    assert set(services["registry"]["networks"]) == {
        "registry-edge",
        "registry-publisher",
    }
    assert set(services["control-worker"]["networks"]) == {"data", "worker-authority"}
    assert set(services["control-api"]["networks"]) == {
        "agent-proxy",
        "application",
        "ca",
        "data",
        "worker-authority",
    }
    assert rendered["networks"]["worker-authority"]["internal"] is True
    assert set(services["litellm"]["networks"]) == {
        "cluster-egress",
        "litellm-data",
        "litellm-edge",
    }
    assert services["litellm"].get("ports") in (None, [])
    assert rendered["networks"]["litellm-edge"]["internal"] is True
    assert rendered["networks"]["litellm-data"]["internal"] is True
    assert {
        name
        for name, service in services.items()
        if "litellm-edge" in service.get("networks", {})
    } == {"caddy", "litellm"}
    assert {
        name
        for name, service in services.items()
        if "litellm-data" in service.get("networks", {})
    } == {"litellm", "postgres"}
    litellm_networks = set(services["litellm"]["networks"])
    for name, service in services.items():
        if name not in {"caddy", "litellm", "postgres"}:
            assert litellm_networks.isdisjoint(service.get("networks", {})), name
    assert set(services["prometheus"]["networks"]) == {"application"}
    for service in ("control-api", "control-worker"):
        assert services[service]["environment"]["VONK_MANAGEMENT_CIDRS"] == (
            "10.0.0.0/24"
        )
        assert services[service]["environment"]["VONK_DIRECT_FABRIC_CIDRS"] == (
            "192.168.100.0/24,192.168.101.0/24"
        )


def test_litellm_runs_the_bind_mounted_entrypoint_through_shell() -> None:
    litellm = _rendered()["services"]["litellm"]

    assert litellm["entrypoint"] == ["/bin/sh", "/app/vonk-entrypoint"]


def test_non_root_runtime_services_use_normalized_secret_volume() -> None:
    services = _rendered()["services"]
    for service in ("control-worker", "litellm", "prometheus", "grafana"):
        assert "normalized-private-keys" in {
            item["source"] for item in services[service]["volumes"]
        }
        assert services[service].get("secrets", []) == []
    assert "normalized-private-keys" in {
        item["source"] for item in services["control-api"]["volumes"]
    }
    assert "admin-password" in {
        item["source"] for item in services["control-api"]["secrets"]
    }
    assert services["litellm"]["environment"]["LITELLM_MASTER_KEY_FILE"] == (
        "/run/vonk-normalized-secrets/litellm-master-key"
    )
    assert services["grafana"]["environment"]["GF_SECURITY_ADMIN_PASSWORD__FILE"] == (
        "/run/vonk-normalized-secrets/grafana-admin-password"
    )


def test_worker_has_a_distinct_minimal_image_and_runtime_boundary() -> None:
    services = _rendered()["services"]
    api = services["control-api"]
    worker = services["control-worker"]

    assert api["image"] != worker["image"]
    assert api["image"].startswith("example/control-api:")
    assert worker["image"].startswith("example/control-worker:")
    assert worker.get("secrets", []) == []
    for service in ("control-api", "control-worker"):
        assert "normalized-private-keys" in {
            item["source"] for item in services[service]["volumes"]
        }
    assert {item["target"] for item in worker["volumes"]} == {
        "/routes",
        "/supervisor",
        "/state",
        "/run/vonk-normalized-secrets",
    }
    assert "VONK_REPOSITORY_PATH" not in worker["environment"]
    assert "VONK_GIT_SIGNING_KEY_FILE" not in worker["environment"]
    assert worker["environment"]["VONK_INTERNAL_API_URL"] == "http://control-api:8000"

    assert "control-signer" not in services
    assert "VONK_UPDATE_SIGNER_SOCKET" not in worker["environment"]


def test_deleted_workload_signer_path_is_absent_from_fresh_graph() -> None:
    services = _rendered()["services"]
    assert "workload-signer" not in services
    assert "workload-signer-socket" not in _rendered()["volumes"]
    assert "VONK_WORKLOAD_SIGNER_SOCKET" not in services["control-api"]["environment"]
    assert not {
        "workload-releases-key",
        "workload-snapshot-key",
        "workload-timestamp-key",
    } & set(_rendered()["secrets"])


def test_control_api_has_only_the_capabilities_required_by_its_preexec() -> None:
    api = _rendered()["services"]["control-api"]

    assert api["user"] == "0:0"
    assert api["cap_drop"] == ["ALL"]
    assert set(api["cap_add"]) == {
        "CHOWN",
        "FOWNER",
        "DAC_OVERRIDE",
        "SETUID",
        "SETGID",
    }
    assert api["security_opt"] == ["no-new-privileges:true"]
    assert "SYS_ADMIN" not in api["cap_add"]
    assert api["command"] == ["python", "-m", "vonk_control.api"]


def test_file_backed_private_keys_are_normalized_by_the_real_api_service() -> None:
    services = _rendered()["services"]
    api = services["control-api"]
    worker = services["control-worker"]

    assert "control-secret-init" not in services
    assert "control-bootstrap" not in services
    assert api["depends_on"]["postgres"] == {
        "condition": "service_healthy",
        "required": True,
    }
    assert "step-ca" not in api["depends_on"]
    api_secrets = {secret["source"] for secret in api["secrets"]}
    assert {
        "package-helper-grant-private-key",
        "package-helper-receipt-private-key",
        "host-runtime-grant-private-key",
        "database-url",
    } <= api_secrets
    normalized = {volume["target"]: volume for volume in api["volumes"]}
    assert normalized["/normalized"].get("read_only") is not True
    assert normalized["/run/vonk-normalized-secrets"]["read_only"] is True
    assert api["environment"]["VONK_PACKAGE_HELPER_GRANT_PRIVATE_KEY_FILE"] == (
        "/run/vonk-normalized-secrets/package-helper-grant-private-key"
    )
    assert api["environment"]["VONK_HOST_RUNTIME_GRANT_PRIVATE_KEY_FILE"] == (
        "/run/vonk-normalized-secrets/host-runtime-grant-private-key"
    )
    for variable, name in (
        ("VONK_DATABASE_URL_FILE", "database-url"),
        ("VONK_TOKEN_SIGNING_KEY_FILE", "token-signing-key"),
        ("VONK_METRICS_TOKEN_FILE", "metrics-token"),
        ("VONK_CONTROLLER_CA_FILE", "controller-ca"),
        ("VONK_WORKER_API_TOKEN_FILE", "worker-api-token"),
    ):
        assert api["environment"][variable] == f"/run/vonk-normalized-secrets/{name}"
    assert worker["environment"]["VONK_DATABASE_URL_FILE"] == (
        "/run/vonk-normalized-secrets/database-url"
    )
    assert worker["environment"]["VONK_WORKER_API_TOKEN_FILE"] == (
        "/run/vonk-normalized-secrets/worker-api-token"
    )


def test_retired_runtime_signer_and_agent_update_surfaces_are_absent() -> None:
    rendered = _rendered()
    serialized = json.dumps(rendered, sort_keys=True)

    assert "control-signer" not in rendered["services"]
    for retired in (
        "agent-tuf",
        "agent-update-authority",
        "admin-grant",
        "signer-tuf",
        "update-signer",
    ):
        assert retired not in serialized


def test_former_bootstrap_dependants_wait_for_real_service_health() -> None:
    services = _rendered()["services"]

    for name in ("control-worker", "litellm", "step-ca"):
        assert services[name]["depends_on"]["control-api"] == {
            "condition": "service_healthy",
            "required": True,
        }




def test_caddy_has_readiness_checks() -> None:
    services = _rendered()["services"]

    assert services["caddy"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "wget -q -O /dev/null http://127.0.0.1:8082/healthz",
    ]


def test_litellm_routes_use_a_dedicated_atomic_config_volume() -> None:
    services = _rendered()["services"]
    worker_volumes = {
        volume["target"]: volume for volume in services["control-worker"]["volumes"]
    }
    api_volumes = {
        volume["target"]: volume for volume in services["control-api"]["volumes"]
    }
    assert "/state/agent-artifacts" in services["control-api"]["tmpfs"]
    assert "/state/agent-artifacts" not in api_volumes
    litellm_volumes = {
        volume["target"]: volume for volume in services["litellm"]["volumes"]
    }

    assert worker_volumes["/routes"]["source"] == "route-publications"
    assert api_volumes["/routes"]["source"] == "route-publications"
    assert api_volumes["/routes"].get("read_only", False) is False
    assert worker_volumes["/supervisor"] == {
        "type": "volume",
        "source": "litellm-supervisor-state",
        "target": "/supervisor",
        "read_only": True,
        "volume": {},
    }
    assert litellm_volumes["/routes"] == {
        "type": "volume",
        "source": "route-publications",
        "target": "/routes",
        "read_only": True,
        "volume": {},
    }
    assert litellm_volumes["/supervisor"]["source"] == "litellm-supervisor-state"
    assert "VONK_LITELLM_CONFIG_PATH" not in services["control-worker"]["environment"]
    assert "litellm-upstream-key" not in {
        secret["source"] for secret in services["control-worker"].get("secrets", [])
    }
    assert services["litellm"]["user"] == "10002:10001"
    assert services["litellm"]["cap_drop"] == ["ALL"]
    assert services["litellm"]["security_opt"] == ["no-new-privileges:true"]


def test_postgres_mounts_the_parent_directory_for_postgres_18() -> None:
    postgres_volumes = _rendered()["services"]["postgres"]["volumes"]

    assert {
        volume["source"]: volume["target"] for volume in postgres_volumes
    }["postgres-data"] == "/var/lib/postgresql"


def test_caddy_disables_admin_and_sets_edge_guards() -> None:
    root = Path(__file__).resolve().parents[3]
    text = (root / "deploy/compose/Caddyfile").read_text()
    assert "admin off" in text
    assert "max_size 1MB" in text
    assert "Strict-Transport-Security" in text
    assert "X-Frame-Options" in text
