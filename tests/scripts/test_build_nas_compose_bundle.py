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
    "api_image": "ghcr.io/carstvaartjes/vonk-forge-api:v1.2.3",
    "worker_image": "ghcr.io/carstvaartjes/vonk-forge-worker:v1.2.3",
    "hermes_image": "ghcr.io/carstvaartjes/vonk-forge-hermes:v1.2.3",
    "litellm_image": "ghcr.io/carstvaartjes/vonk-forge-litellm:v1.2.3",
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
        TEMPLATE, output, **IMAGES, channel="pinned"
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
    # Only these lack a fixed default (the management CIDRs then derive from the
    # NAS address); every other value is defaulted or generated, never asked.
    assert [
        item["env"] for item in payload["required_values"] if item["default"] is None
    ] == ["NAS_LAN_IP", "VONK_MANAGEMENT_CIDRS", "VONK_CONTROL_HOSTNAME"]
    assert {item["file"] for item in payload["secrets"]} == {
        "tailscale-oauth-client-id",
        "tailscale-oauth-client-secret",
        "litellm-upstream-key",
        "hf-token",
    }
    optional = {item["file"] for item in payload["secrets"] if item.get("optional")}
    assert optional == {"litellm-upstream-key", "hf-token"}
    assert {item["env"] for item in payload["install_modes"]["lab_values"]} == {
        "VONK_CONTROL_HOSTNAME"
    }
    generated = payload["generated_secrets"]
    assert all("prompt" not in item for group in generated.values() for item in group)
    hermes_key = next(
        item
        for item in generated["random_text"]
        if item["file"] == "hermes-litellm-key"
    )
    assert hermes_key["prefix"] == "sk-"
    assert payload["step_ca_controller"]["hostname_env"] == "VONK_CONTROL_HOSTNAME"
    assert set(payload["hermes"]) == {
        "env",
        "prompt",
        "enabled_value",
        "disabled_value",
    }
    # Runtime configuration ships in the Controller image, not the bundle.
    assert "runtime_files" not in payload
    assert "configs" not in original_compose

    installer_secret_files = {item["file"] for item in payload["secrets"]}
    for group in ("random_text", "ed25519_pkcs8_pem", "postgres_urls"):
        installer_secret_files.update(
            item["file"] for item in payload["generated_secrets"][group]
        )
    installer_secret_files.update(payload["step_ca_controller"]["files"].values())

    installer_environment = {item["env"] for item in payload["internal_values"]} | {
        item["env"] for item in payload["required_values"]
    }
    installer_environment.add(payload["hermes"]["env"])

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
    assert compose["services"]["tailscale-gateway"]["profiles"] == ["secure-remote"]
    assert compose["services"]["tailscale-configurator"]["profiles"] == [
        "secure-remote"
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
    assert "configs" not in compose
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


@pytest.mark.parametrize("mutation", ("service", "image", "include", "bind", "config"))
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
        document["configs"] = {"caddyfile": {"file": "./Caddyfile"}}
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


@pytest.mark.parametrize("channel", ("dev", "stable"))
def test_installer_compose_follows_the_channel_and_keeps_third_party_pins(
    tmp_path: Path, channel: str
) -> None:
    document = yaml.safe_load(_render(tmp_path).read_text())
    builder = _load(SCRIPT, "channel_bundle_builder")
    payload = builder._payload(document, channel)
    services = yaml.safe_load(payload["docker_compose_yaml"])["services"]
    lock = json.loads((ROOT / "deploy/compose/images.lock.json").read_text())
    for service in services.values():
        image = service["image"]
        if image.startswith("ghcr.io/carstvaartjes/vonk-forge-"):
            assert image.endswith(":dev" if channel == "dev" else ":latest")
            assert service["pull_policy"] == "always"
        else:
            # A third-party image stays on the version the release was tested
            # with; a new upstream major never arrives through a channel tag.
            assert image in lock["images"].values()


def test_only_the_configured_postgres_backup_mount_is_allowed(tmp_path: Path) -> None:
    rendered = _render(tmp_path)
    document = yaml.safe_load(rendered.read_text(encoding="utf-8"))
    builder = _load(SCRIPT, "backup_mount_bundle_builder")

    builder._validate_services(document)
    document["services"]["litellm"].setdefault("volumes", []).append(
        {"type": "bind", "source": "./untrusted", "target": "/untrusted"}
    )
    with pytest.raises(builder.BundleError, match="host bind mount"):
        builder._validate_services(document)
