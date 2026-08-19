import json
import os
import subprocess
import sys
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
        "VONK_CONTROL_GENERATION_ID": "gen-" + "2" * 24,
        "VONK_DATABASE_REVISION": "0012_control_process_heartbeats",
        "VONK_CONTROL_START_NONCE": "4" * 64,
        "GRAFANA_ADMIN_PASSWORD_FILE": "/dev/null",
        "LITELLM_MASTER_KEY_FILE": "/dev/null",
        "LITELLM_UPSTREAM_KEY_FILE": "/dev/null",
        "LITELLM_DATABASE_URL_FILE": "/dev/null",
        "STEP_CA_IMAGE": "smallstep/step-ca:0.30.2@sha256:" + "1" * 64,
        "TAILSCALE_IMAGE": "tailscale/tailscale:v1.98.8@sha256:d54b2e6a9c09f0e5ec52e82b9ad4af3d446b54a7c08075e92f11c39dd410105f",
        "AGENT_CLIENT_CA_FILE": "/dev/null",
        "AGENT_INTERMEDIATE_CERTIFICATE_FILE": "/dev/null",
        "CONTROLLER_CA_FILE": "/dev/null",
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
            "-f",
            str(root / "deploy/compose/compose.step-ca.yaml"),
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


def test_worker_has_a_distinct_minimal_image_and_runtime_boundary() -> None:
    services = _rendered()["services"]
    api = services["control-api"]
    worker = services["control-worker"]

    assert api["image"] != worker["image"]
    assert api["image"].startswith("example/control-api:")
    assert worker["image"].startswith("example/control-worker:")
    worker_secrets = {item["source"] for item in worker["secrets"]}
    assert worker_secrets == {"database-url", "worker-api-token"}
    api_secrets = {item["source"] for item in api["secrets"]}
    assert "agent-update-authority-key" not in api_secrets
    assert "admin-grant-private-key" not in api_secrets
    assert {item["source"]: item.get("read_only", False) for item in api["volumes"]}[
        "api-admin-grant-runtime"
    ] is True
    assert {item["target"] for item in worker["volumes"]} == {
        "/routes",
        "/supervisor",
        "/state",
        "/run/vonk-signer",
        "/run/vonk-forge/control-identity",
    }
    assert "VONK_REPOSITORY_PATH" not in worker["environment"]
    assert "VONK_GIT_SIGNING_KEY_FILE" not in worker["environment"]
    assert worker["environment"]["VONK_INTERNAL_API_URL"] == "http://control-api:8000"

    signer = services["control-signer"]
    assert signer["network_mode"] == "none"
    assert signer["user"] == "10003:10001"
    assert {item["source"] for item in signer["secrets"]} == {
        "admin-grant-public-key",
        "agent-tuf-bootstrap-root",
        "agent-update-authority-key",
    }
    assert "database-url" not in {item["source"] for item in signer["secrets"]}
    assert {item["target"] for item in signer["volumes"]} == {
        "/control-identity",
        "/publication",
        "/run/vonk-signer",
        "/verifier",
    }
    bootstrap = services["control-bootstrap"]
    assert bootstrap["network_mode"] == "none"
    assert any(
        item.get("source") == "update-signer-socket"
        and item.get("target") == "/update-socket"
        for item in bootstrap["volumes"]
    )


def test_workload_signer_isolated_from_api_and_platform_signer() -> None:
    services = _rendered()["services"]
    signer = services["workload-signer"]
    assert signer["network_mode"] == "none"
    assert signer["user"] == "10003:10001"
    assert signer["cap_drop"] == ["ALL"]
    assert {item["source"] for item in signer["secrets"]} == {
        "workload-releases-key",
        "workload-snapshot-key",
        "workload-timestamp-key",
    }
    api = services["control-api"]
    assert not {
        "workload-releases-key",
        "workload-snapshot-key",
        "workload-timestamp-key",
    } & {item["source"] for item in api["secrets"]}
    assert services["control-bootstrap"]["network_mode"] == "none"
    assert services["control-signer"]["secrets"] != signer["secrets"]


def test_selected_services_reopen_the_root_owned_identity_directory_read_only() -> None:
    rendered = _rendered()
    services = rendered["services"]
    expected_mount = {
        "type": "bind",
        "source": "/srv/vonk-forge/control-identity",
        "target": "/run/vonk-forge/control-identity",
        "read_only": True,
        "bind": {"create_host_path": False},
    }

    assert "signer-activation-init" not in services
    assert not any(
        name.endswith("signer-active-control") for name in rendered["volumes"]
    )
    for service_name in ("control-api", "control-worker"):
        service = services[service_name]
        mounts = {volume["target"]: volume for volume in service["volumes"]}
        assert mounts["/run/vonk-forge/control-identity"] == expected_mount
        assert not any(
            "control-host" in volume.get("source", "")
            or "control-generations" in volume.get("source", "")
            for volume in service["volumes"]
        )

    assert services["control-api"]["environment"]["VONK_CONTROL_IDENTITY_ROOT"] == (
        "/run/vonk-forge/control-identity"
    )
    assert services["control-worker"]["environment"]["VONK_CONTROL_IDENTITY_ROOT"] == (
        "/run/vonk-forge/control-identity"
    )
    signer = services["control-signer"]
    signer_mounts = {volume["target"]: volume for volume in signer["volumes"]}
    assert signer_mounts["/control-identity"] == expected_mount | {
        "target": "/control-identity"
    }
    assert signer["environment"]["VONK_CONTROL_IDENTITY_ROOT"] == "/control-identity"
    assert signer["environment"]["VONK_CONTROL_PROCESS_IMAGE"] == signer["image"]
    assert "VONK_ACTIVE_CONTROL_STATE_ROOT" not in signer["environment"]


def test_selected_api_and_worker_receive_one_dynamic_exact_generation_identity() -> (
    None
):
    services = _rendered()["services"]
    api = services["control-api"]
    worker = services["control-worker"]
    common = {
        "VONK_CONTROL_STARTUP_MODE": "selected",
        "VONK_CONTROL_GENERATION_ID": "gen-" + "2" * 24,
        "VONK_DATABASE_REVISION": "0012_control_process_heartbeats",
        "VONK_PLATFORM_VERSION": "1.0.0",
        "VONK_PLATFORM_RELEASE_DIGEST": "sha256:" + "2" * 64,
        "VONK_PLATFORM_BUILD_DIGEST": "sha256:" + "3" * 64,
        "VONK_CONTROL_START_NONCE": "4" * 64,
    }

    for service in (api, worker):
        assert common.items() <= service["environment"].items()
    assert api["environment"]["VONK_CONTROL_PROCESS_IMAGE"] == api["image"]
    assert worker["environment"]["VONK_CONTROL_PROCESS_IMAGE"] == worker["image"]


def test_bootstrap_prepares_signer_directories() -> None:
    bootstrap = _rendered()["services"]["control-bootstrap"]
    assert "DAC_OVERRIDE" in bootstrap["cap_add"]
    assert bootstrap["command"] == ["python", "-m", "vonk_control.compose_bootstrap"]
    assert bootstrap["healthcheck"]["test"] == ["CMD", "test", "-f", "/tmp/bootstrap-ready"]


def test_file_backed_private_keys_are_normalized_before_bootstrap() -> None:
    services = _rendered()["services"]
    init = services["control-secret-init"]
    api = services["control-api"]
    bootstrap = services["control-bootstrap"]

    assert init["command"] == ["python", "-m", "vonk_control.compose_secret_init"]
    assert init["secrets"] == [
        {"source": "admin-grant-private-key", "target": "/run/secrets/admin-grant-private-key"},
        {"source": "package-helper-grant-private-key", "target": "/run/secrets/package-helper-grant-private-key"},
        {"source": "package-helper-receipt-private-key", "target": "/run/secrets/package-helper-receipt-private-key"},
        {"source": "host-runtime-grant-private-key", "target": "/run/secrets/host-runtime-grant-private-key"},
    ]
    assert bootstrap["depends_on"]["control-secret-init"] == {
        "condition": "service_completed_successfully",
        "required": True,
    }
    assert api["environment"]["VONK_PACKAGE_HELPER_GRANT_PRIVATE_KEY_FILE"] == (
        "/run/vonk-normalized-secrets/package-helper-grant-private-key"
    )
    assert api["environment"]["VONK_HOST_RUNTIME_GRANT_PRIVATE_KEY_FILE"] == (
        "/run/vonk-normalized-secrets/host-runtime-grant-private-key"
    )


def test_admin_grant_signing_key_is_available_only_to_bootstrap() -> None:
    services = _rendered()["services"]
    secret_sources = {
        name: {secret["source"] for secret in services[name]["secrets"]}
        for name in ("control-api", "control-worker", "control-signer")
    }
    assert "admin-grant-private-key" not in secret_sources["control-api"]
    assert "admin-grant-private-key" not in secret_sources["control-worker"]
    assert "admin-grant-private-key" not in secret_sources["control-signer"]
    assert "admin-grant-private-key" in {
        secret["source"] for secret in services["control-secret-init"]["secrets"]
    }




def test_caddy_has_readiness_checks() -> None:
    services = _rendered()["services"]

    assert services["caddy"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "wget -q -O /dev/null http://127.0.0.1:8080/healthz",
    ]


def test_litellm_routes_use_a_dedicated_atomic_config_volume() -> None:
    services = _rendered()["services"]
    worker_volumes = {
        volume["target"]: volume for volume in services["control-worker"]["volumes"]
    }
    api_volumes = {
        volume["target"]: volume for volume in services["control-api"]["volumes"]
    }
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
        secret["source"] for secret in services["control-worker"]["secrets"]
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
