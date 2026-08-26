from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build-nas-compose-bundle"
RENDERER = ROOT / "scripts/render-production-compose"
TEMPLATE = ROOT / "deploy/compose/compose.yaml"
DIGEST = "a" * 64
IMAGES = {
    "api_image": f"ghcr.io/carstvaartjes/vonk-forge-api:v1.2.3@sha256:{DIGEST}",
    "worker_image": f"ghcr.io/carstvaartjes/vonk-forge-worker:v1.2.3@sha256:{DIGEST}",
    "hermes_image": f"ghcr.io/carstvaartjes/vonk-forge-hermes:v1.2.3@sha256:{DIGEST}",
}
SERVICES = {
    "tailscale-gateway",
    "tailscale-configurator",
    "hermes-agent",
    "hermes-litellm-key-provisioner",
    "postgres",
    "control-api",
    "control-worker",
    "step-ca",
    "litellm",
    "prometheus",
    "grafana",
    "caddy",
    "registry",
}


def _load(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _render(tmp_path: Path) -> Path:
    output = tmp_path / "docker-compose.yaml"
    _load(RENDERER, "nas_payload_production_renderer").render(
        TEMPLATE, output, **IMAGES
    )
    return output


def _build(compose: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--compose",
            str(compose),
            "--output",
            str(output),
            "--channel",
            "stable",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_payload_is_complete_self_contained_and_fresh_install_only(
    tmp_path: Path,
) -> None:
    output = tmp_path / "payload.json"
    rendered = _render(tmp_path)
    original_compose = yaml.safe_load(rendered.read_text(encoding="utf-8"))
    result = _build(rendered, output)

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["internal_values"] == [
        {"env": "COMPOSE_PROJECT_NAME", "value": "vonk-forge-control"},
        {"env": "VONK_INSTALL_CHANNEL", "value": "stable"},
    ]
    assert {item["env"] for item in payload["required_values"]} == {
        "NAS_LAN_IP",
        "VONK_MANAGEMENT_CIDRS",
        "VONK_DIRECT_FABRIC_CIDRS",
        "VONK_CONTROL_HOSTNAME",
        "VONK_AGENT_ENROLL_HOSTNAME",
        "VONK_AGENT_HOSTNAME",
        "VONK_REGISTRY_HOSTNAME",
    }
    validators = {
        item["env"]: item["validation"] for item in payload["required_values"]
    }
    assert validators == {
        "NAS_LAN_IP": "ipv4",
        "VONK_MANAGEMENT_CIDRS": "cidr_list",
        "VONK_DIRECT_FABRIC_CIDRS": "optional_cidr_list",
        "VONK_CONTROL_HOSTNAME": "hostname",
        "VONK_AGENT_ENROLL_HOSTNAME": "hostname",
        "VONK_AGENT_HOSTNAME": "hostname",
        "VONK_REGISTRY_HOSTNAME": "hostname",
    }
    assert {item["file"] for item in payload["secrets"]} == {
        "admin-password",
        "tailscale-oauth-client-id",
        "tailscale-oauth-client-secret",
        "litellm-upstream-key",
    }
    secret_prompts = {item["file"]: item for item in payload["secrets"]}
    assert secret_prompts["admin-password"]["generate_bytes"] == 24
    for external in (
        "tailscale-oauth-client-id",
        "tailscale-oauth-client-secret",
        "litellm-upstream-key",
    ):
        assert secret_prompts[external]["generate_bytes"] is None
    generated = payload["generated_secrets"]
    assert {item["file"] for item in generated["random_text"]} == {
        "postgres-password",
        "litellm-database-password",
        "token-signing-key",
        "metrics-token",
        "litellm-master-key",
        "grafana-admin-password",
        "agent-proxy-auth",
        "worker-api-token",
        "hermes-api-key",
    }
    assert {item["file"] for item in generated["ed25519_pkcs8_pem"]} == {
        "package-helper-grant-private-key.pem",
        "package-helper-receipt-private-key.pem",
        "host-runtime-grant-private-key.pem",
    }
    assert {item["file"] for item in generated["postgres_urls"]} == {
        "database-url",
        "litellm-database-url",
    }
    assert payload["step_ca_controller"]["kid_env"] == "AGENT_CA_PROVISIONER_KID"
    assert payload["hermes"] == {
        "env": "COMPOSE_PROFILES",
        "prompt": "Enable the optional Hermes agent?",
        "enabled_value": "hermes",
        "disabled_value": "",
        "required_values": [
            {
                "env": "HERMES_DASHBOARD_ORIGIN",
                "prompt": "Hermes dashboard HTTPS origin",
                "default": None,
                "validation": "https_origin",
            }
        ],
        "secrets": [
            {
                "file": "hermes-litellm-key",
                "prompt": "Dedicated Hermes LiteLLM client key",
                "generate_bytes": 32,
                "prefix": "sk-",
            }
        ],
    }
    runtime_files = {item["file"]: item for item in payload["runtime_files"]}
    assert len(runtime_files) == len(original_compose["configs"])

    installer_secret_files = {item["file"] for item in payload["secrets"]}
    for group in ("random_text", "ed25519_pkcs8_pem", "postgres_urls"):
        installer_secret_files.update(
            item["file"] for item in payload["generated_secrets"][group]
        )
    installer_secret_files.update(payload["step_ca_controller"]["files"].values())
    installer_environment = {item["env"] for item in payload["internal_values"]} | {
        item["env"] for item in payload["required_values"]
    }
    installer_environment.add(payload["step_ca_controller"]["kid_env"])
    installer_environment.add(payload["hermes"]["env"])
    installer_environment.update(
        item["env"] for item in payload["hermes"]["required_values"]
    )
    installer_secret_files.update(item["file"] for item in payload["hermes"]["secrets"])

    compose_text = payload["docker_compose_yaml"]
    compose = yaml.safe_load(compose_text)
    required_compose_environment = set(
        re.findall(r"(?<!\$)\$\{([A-Z_][A-Z0-9_]*):\?", compose_text)
    )
    assert required_compose_environment <= installer_environment
    assert set(compose["services"]) == SERVICES
    assert "include" not in compose
    assert "name" not in compose
    assert "version" not in compose
    assert all("build" not in service for service in compose["services"].values())
    assert compose["services"]["hermes-agent"]["profiles"] == ["hermes"]
    assert compose["services"]["hermes-litellm-key-provisioner"]["profiles"] == [
        "hermes"
    ]
    assert compose["services"]["caddy"]["ports"] == [
        {
            "target": 8443,
            "published": 8443,
            "host_ip": "${NAS_LAN_IP:?set reserved NAS LAN IP}",
            "protocol": "tcp",
        }
    ]
    assert all(
        secret["file"].startswith("./secrets/") and "${" not in secret["file"]
        for secret in compose["secrets"].values()
    )
    compose_secret_files = {
        secret["file"].removeprefix("./secrets/")
        for secret in compose["secrets"].values()
    }
    assert compose_secret_files == installer_secret_files
    assert all(set(config) == {"file"} for config in compose["configs"].values())
    compose_runtime_files = {
        config["file"].removeprefix("./secrets/")
        for config in compose["configs"].values()
    }
    assert compose_runtime_files == set(runtime_files)
    for name, original in original_compose["configs"].items():
        relative = compose["configs"][name]["file"].removeprefix("./secrets/")
        assert runtime_files[relative]["content"] == original["content"].replace(
            "$$", "$"
        )
        referenced_modes = {
            reference.get("mode", "0444")
            for service in original_compose["services"].values()
            for reference in service.get("configs", [])
            if reference["source"] == name
        }
        assert runtime_files[relative]["mode"] == (
            0o755 if referenced_modes == {"0555"} else 0o644
        )
    assert all(
        set(reference) <= {"source", "target"}
        for service in compose["services"].values()
        for reference in service.get("configs", [])
    )
    assert all(
        service.get("read_only") is True
        for service in compose["services"].values()
        if service.get("configs") and service.get("read_only") is not None
    )
    assert compose["secrets"]["step-ca-config"]["file"] == ("./secrets/step-ca/ca.json")
    assert all(
        "STEP_CA_CONFIG_FILE" not in str(volume)
        for volume in compose["services"]["step-ca"]["volumes"]
    )
    assert "control-secret-init" not in compose_text
    assert "/repository" not in compose_text
    assert "migrate" not in compose_text.lower()
    assert "supervisor" not in set(compose["services"])
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert result.stdout.startswith("sha256:")


def test_payload_build_is_deterministic_and_refuses_to_overwrite(
    tmp_path: Path,
) -> None:
    compose = _render(tmp_path)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert _build(compose, first).returncode == 0
    assert _build(compose, second).returncode == 0
    assert first.read_bytes() == second.read_bytes()

    preserved = tmp_path / "preserved.json"
    preserved.write_text("operator data\n", encoding="utf-8")
    result = _build(compose, preserved)
    assert result.returncode == 2
    assert preserved.read_text(encoding="utf-8") == "operator data\n"


@pytest.mark.parametrize(
    "mutation", ("service", "image", "include", "bind", "config-dollar")
)
def test_payload_build_rejects_noncanonical_compose(
    tmp_path: Path, mutation: str
) -> None:
    compose = _render(tmp_path)
    document = yaml.safe_load(compose.read_text(encoding="utf-8"))
    if mutation == "service":
        document["services"]["legacy-updater"] = {
            "image": "busybox:1@sha256:" + "b" * 64
        }
    elif mutation == "image":
        document["services"]["control-api"]["image"] = "example/api:latest"
    elif mutation == "include":
        document["include"] = ["other.yaml"]
    elif mutation == "bind":
        document["services"]["control-api"]["volumes"].append(
            "./repository:/repository:ro"
        )
    else:
        first_config = next(iter(document["configs"].values()))
        first_config["content"] += "\n$UNSAFE_INTERPOLATION\n"
    compose.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    output = tmp_path / f"{mutation}.json"
    result = _build(compose, output)

    assert result.returncode == 2
    assert not output.exists()


def test_payload_build_rejects_symlink_input(tmp_path: Path) -> None:
    compose = _render(tmp_path)
    link = tmp_path / "compose-link.yaml"
    link.symlink_to(compose)

    result = _build(link, tmp_path / "payload.json")

    assert result.returncode == 2
    assert not (tmp_path / "payload.json").exists()
