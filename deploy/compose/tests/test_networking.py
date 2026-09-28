import json
import os
import subprocess
from pathlib import Path


def _rendered() -> dict:
    root = Path(__file__).resolve().parents[3]
    env = os.environ | dict(
        line.split("=", 1)
        for line in (root / "deploy/compose/tests/test.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
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


def test_only_caddy_publishes_ports_and_images_are_version_pinned() -> None:
    rendered = _rendered()
    published = {
        name for name, service in rendered["services"].items() if service.get("ports")
    }
    assert published == {"caddy"}
    floating = {"latest", "main", "edge", "stable", "master", "dev"}
    assert all(
        "@sha256:" in service["image"]
        or service["image"].rsplit(":", 1)[-1].lower() not in floating
        or service.get("build")
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
    assert caddy["environment"] == {"VONK_CONTROL_HOSTNAME": "control.test.example"}


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
    assert set(services["control-worker"]["networks"]) == {"data", "artifact-egress"}
    assert not services["control-worker"].get("ports")
    assert not rendered["networks"]["artifact-egress"].get("internal", False)
    assert {
        name
        for name, service in services.items()
        if "artifact-egress" in service.get("networks", {})
    } == {"control-worker"}
    assert set(services["control-api"]["networks"]) == {
        "agent-proxy",
        "application",
        "ca",
        "data",
    }
    # Relays, secret paths, and tuning are fixed in code, not configuration.
    assert set(services["control-api"]["environment"]) == {
        "VONK_CONTROL_HOSTNAME",
        "VONK_NAS_LAN_IP",
        "VONK_MANAGEMENT_CIDRS",
        "VONK_DIRECT_FABRIC_CIDRS",
        "VONK_INSTALL_CHANNEL",
        "VONK_RECIPE_LIBRARY_RELEASE",
    }
    assert rendered["networks"]["ingress"].get("internal", False) is False
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
    # The worker reads the same site configuration as the API.
    assert (
        services["control-worker"]["environment"]
        == services["control-api"]["environment"]
    )


def test_litellm_runs_the_docker_staged_entrypoint_through_shell() -> None:
    litellm = _rendered()["services"]["litellm"]

    assert litellm["entrypoint"] == [
        "/bin/sh",
        "/run/vonk-normalized-secrets/runtime-assets/litellm/entrypoint.sh",
    ]


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
        "/state/agent-artifacts",
        "/run/vonk-normalized-secrets",
    }
    assert "VONK_REPOSITORY_PATH" not in worker["environment"]
    assert "VONK_GIT_SIGNING_KEY_FILE" not in worker["environment"]

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

    assert "control-secret-init" not in services
    assert "control-bootstrap" not in services
    assert api["depends_on"]["postgres"] == {
        "condition": "service_healthy",
        "required": True,
    }
    assert "step-ca" not in api["depends_on"]
    api_secrets = {secret["source"] for secret in api["secrets"]}
    assert {
        "host-runtime-grant-private-key",
        "database-url",
        "hf-token",
    } <= api_secrets
    normalized = {volume["target"]: volume for volume in api["volumes"]}
    assert normalized["/normalized"].get("read_only") is not True
    assert normalized["/run/vonk-normalized-secrets"]["read_only"] is True


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


def test_recipe_images_use_a_dedicated_persistent_volume() -> None:
    rendered = _rendered()
    api = rendered["services"]["control-api"]
    worker = rendered["services"]["control-worker"]
    api_volumes = {volume["target"]: volume for volume in api["volumes"]}
    worker_volumes = {volume["target"]: volume for volume in worker["volumes"]}

    assert api["tmpfs"] == ["/tmp"]
    expected_artifact_volume = {
        "type": "volume",
        "source": "agent-artifacts",
        "target": "/state/agent-artifacts",
        "volume": {},
    }
    assert api_volumes["/state/agent-artifacts"] == expected_artifact_volume
    assert worker_volumes["/state/agent-artifacts"] == expected_artifact_volume
    assert "agent-artifacts" in rendered["volumes"]


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
        secret["source"] for secret in services["control-worker"].get("secrets", [])
    }
    assert services["litellm"]["user"] == "10002:10001"
    assert services["litellm"]["cap_drop"] == ["ALL"]
    assert services["litellm"]["security_opt"] == ["no-new-privileges:true"]


def test_postgres_mounts_the_parent_directory_for_postgres_18() -> None:
    postgres_volumes = _rendered()["services"]["postgres"]["volumes"]

    assert {volume["source"]: volume["target"] for volume in postgres_volumes}[
        "postgres-data"
    ] == "/var/lib/postgresql"


def test_caddy_disables_admin_and_sets_edge_guards() -> None:
    root = Path(__file__).resolve().parents[3]
    text = (root / "deploy/compose/Caddyfile").read_text()
    assert "admin off" in text
    assert "max_size 1MB" in text
    assert "source-bundles" not in text
    assert "Strict-Transport-Security" in text
    assert "X-Frame-Options" in text
