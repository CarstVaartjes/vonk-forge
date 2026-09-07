from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy/compose/compose.yaml"
DIGEST = "a" * 64
API_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-{'a' * 40}@sha256:{DIGEST}"
WORKER_IMAGE = (
    f"ghcr.io/carstvaartjes/vonk-forge-worker:dev-sha-{'a' * 40}@sha256:{DIGEST}"
)
HERMES_IMAGE = (
    f"ghcr.io/carstvaartjes/vonk-forge-hermes:dev-sha-{'a' * 40}@sha256:{DIGEST}"
)
LITELLM_IMAGE = (
    f"ghcr.io/carstvaartjes/vonk-forge-litellm:dev-sha-{'a' * 40}@sha256:{DIGEST}"
)
DEV_API_IMAGE = "ghcr.io/carstvaartjes/vonk-forge-api:dev"
DEV_WORKER_IMAGE = "ghcr.io/carstvaartjes/vonk-forge-worker:dev"


SCRIPT = ROOT / "scripts/render-dev-compose"


def _renderer_module() -> Any:
    loader = importlib.machinery.SourceFileLoader("render_dev_compose", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _run_renderer(
    output: Path,
    *,
    api_image: str = API_IMAGE,
    worker_image: str = WORKER_IMAGE,
    hermes_image: str = HERMES_IMAGE,
    litellm_image: str = LITELLM_IMAGE,
    channel: str = "pinned",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--template",
            str(TEMPLATE),
            "--output",
            str(output),
            "--api-image",
            api_image,
            "--worker-image",
            worker_image,
            "--hermes-image",
            hermes_image,
            "--litellm-image",
            litellm_image,
            "--channel",
            channel,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_render_embeds_source_owned_runtime_assets_in_a_single_compose_file(
    tmp_path: Path,
) -> None:
    """Catches a deployment bundle that needs files beside docker-compose.yaml."""
    output = tmp_path / "docker-compose.yaml"

    result = _run_renderer(output)

    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert "\ninclude:" not in text
    assert "tailscale-gateway" in document["services"]
    assert "hermes-agent" in document["services"]
    assert document["services"]["hermes-agent"]["profiles"] == ["hermes"]
    assert document["services"]["hermes-litellm-key-provisioner"]["profiles"] == [
        "hermes"
    ]
    assert document["services"]["control-api"]["image"] == API_IMAGE
    assert document["services"]["control-worker"]["image"] == WORKER_IMAGE
    assert document["services"]["litellm"]["image"] == LITELLM_IMAGE
    assert {path.name for path in tmp_path.iterdir()} == {"docker-compose.yaml"}
    assert document["services"]["caddy"]["configs"]
    assert "configs:" in text
    assert all(
        isinstance(service, dict)
        and isinstance(service.get("image"), str)
        and "@sha256:" in service["image"]
        and "${" not in service["image"]
        for service in document["services"].values()
    )

    for profile in ([], ["--profile", "hermes"]):
        config = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ROOT / "deploy/compose/tests/test.env"),
                "-f",
                str(output),
                *profile,
                "config",
                "-q",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert config.returncode == 0, config.stderr


def test_runtime_config_identity_changes_with_content(tmp_path: Path) -> None:
    renderer = _renderer_module()
    source = tmp_path / "runtime.sh"
    source.write_text("#!/bin/sh\nprintf first\\n\n", encoding="utf-8")
    first = renderer._runtime_config_name(source)
    source.write_text("#!/bin/sh\nprintf second\\n\n", encoding="utf-8")
    second = renderer._runtime_config_name(source)

    assert first.startswith("vonk_runtime_")
    assert second.startswith("vonk_runtime_")
    assert first != second


def test_render_preserves_runtime_asset_executability_with_safe_config_modes(
    tmp_path: Path,
) -> None:
    """Catches embedded scripts becoming non-executable or data becoming writable."""
    output = tmp_path / "docker-compose.yaml"

    result = _run_renderer(output)

    assert result.returncode == 0, result.stderr
    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    expected = {
        "caddy": {
            "/etc/caddy/Caddyfile": "0444",
            "/usr/local/bin/vonk-caddy-entrypoint": "0555",
        },
        "control-api": {
            "/run/vonk-source-assets/litellm/bootstrap-config.json": "0444",
            "/run/vonk-source-assets/litellm/entrypoint.sh": "0555",
            "/run/vonk-source-assets/litellm/config_supervisor.py": "0555",
            "/run/vonk-source-assets/prometheus/prometheus.yml": "0444",
            "/run/vonk-source-assets/prometheus/alerts.yaml": "0444",
        },
        "tailscale-configurator": {
            "/usr/local/bin/configure-tailscale": "0555",
        },
        "hermes-litellm-key-provisioner": {
            "/usr/local/bin/provision-hermes-litellm-key": "0444",
        },
        "postgres": {
            "/docker-entrypoint-initdb.d/10-vonk-forge-databases.sh": "0555",
        },
    }

    for service_name, expected_modes in expected.items():
        mounts = document["services"][service_name]["configs"]
        actual_modes = {
            mount["target"]: mount.get("mode")
            for mount in mounts
            if mount["target"] in expected_modes
        }
        assert actual_modes == expected_modes, service_name


def test_render_uses_canonical_template_and_inlines_step_ca(tmp_path: Path) -> None:
    output = tmp_path / "docker-compose.yaml"

    result = _run_renderer(output)

    assert result.returncode == 0, result.stderr
    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert "step-ca" in document["services"]
    api_secrets = document["services"]["control-api"]["secrets"]
    assert "step-ca-password" in api_secrets


def test_render_rejects_the_mutable_development_image_alias(tmp_path: Path) -> None:
    """Catches a development bundle that is not reproducible from its manifest."""
    output = tmp_path / "docker-compose.yaml"

    result = _run_renderer(
        output,
        api_image=f"ghcr.io/carstvaartjes/vonk-forge-api:dev@sha256:{DIGEST}",
        channel="dev",
    )

    assert result.returncode != 0
    assert "immutable published development image" in result.stderr


def test_render_dev_floats_every_service_and_always_pulls(
    tmp_path: Path,
) -> None:
    output = tmp_path / "docker-compose.yaml"

    result = _run_renderer(
        output,
        api_image=DEV_API_IMAGE,
        worker_image=DEV_WORKER_IMAGE,
        channel="dev",
    )

    assert result.returncode == 0, result.stderr
    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    services = document["services"]
    assert services["control-api"]["image"] == DEV_API_IMAGE
    assert services["control-api"]["pull_policy"] == "always"
    assert services["control-worker"]["image"] == DEV_WORKER_IMAGE
    assert services["control-worker"]["pull_policy"] == "always"
    assert (
        services["hermes-agent"]["image"]
        == "ghcr.io/carstvaartjes/vonk-forge-hermes:dev"
    )
    assert (
        services["litellm"]["image"] == "ghcr.io/carstvaartjes/vonk-forge-litellm:dev"
    )
    for service in services.values():
        expected_tag = (
            "dev"
            if service["image"].startswith("ghcr.io/carstvaartjes/vonk-forge-")
            else "latest"
        )
        assert service["image"].endswith(f":{expected_tag}")
        assert "@" not in service["image"]
        assert service["pull_policy"] == "always"


def test_render_dev_rejects_role_swapped_mutable_aliases(tmp_path: Path) -> None:
    output = tmp_path / "docker-compose.yaml"

    result = _run_renderer(
        output,
        api_image=DEV_WORKER_IMAGE,
        worker_image=DEV_API_IMAGE,
        channel="dev",
    )

    assert result.returncode != 0
    assert "immutable published development image" in result.stderr
