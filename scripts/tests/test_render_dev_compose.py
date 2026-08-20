from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest
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


def _renderer():
    script = ROOT / "scripts/render-dev-compose"
    loader = importlib.machinery.SourceFileLoader("render_dev_compose", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_render_embeds_source_owned_runtime_assets_in_a_single_compose_file(
    tmp_path: Path,
) -> None:
    """Catches a deployment bundle that needs files beside docker-compose.yaml."""
    output = tmp_path / "docker-compose.yaml"

    _renderer().render(
        TEMPLATE, output, API_IMAGE, WORKER_IMAGE, HERMES_IMAGE, channel="pinned"
    )

    text = output.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert "\ninclude:" not in text
    assert "tailscale-gateway" in document["services"]
    assert "hermes-agent" in document["services"]
    assert document["services"]["hermes-agent"]["profiles"] == ["hermes"]
    assert document["services"]["control-api"]["image"] == API_IMAGE
    assert document["services"]["control-worker"]["image"] == WORKER_IMAGE
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


def test_render_preserves_runtime_asset_executability_with_safe_config_modes(
    tmp_path: Path,
) -> None:
    """Catches embedded scripts becoming non-executable or data becoming writable."""
    output = tmp_path / "docker-compose.yaml"

    _renderer().render(
        TEMPLATE, output, API_IMAGE, WORKER_IMAGE, HERMES_IMAGE, channel="pinned"
    )

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    expected = {
        "caddy": {
            "/etc/caddy/Caddyfile": "0444",
            "/usr/local/bin/vonk-caddy-entrypoint": "0555",
        },
        "litellm": {
            "/app/bootstrap-config.json": "0444",
            "/app/vonk-entrypoint": "0555",
            "/app/config-supervisor.py": "0555",
        },
        "tailscale-configurator": {
            "/usr/local/bin/configure-tailscale": "0555",
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

    _renderer().render(
        TEMPLATE,
        output,
        API_IMAGE,
        WORKER_IMAGE,
        HERMES_IMAGE,
        channel="pinned",
    )

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert "step-ca" in document["services"]
    api_secrets = document["services"]["control-api"]["secrets"]
    assert "step-ca-password" in api_secrets


def test_render_rejects_the_mutable_development_image_alias(tmp_path: Path) -> None:
    """Catches a development bundle that is not reproducible from its manifest."""
    output = tmp_path / "docker-compose.yaml"

    with pytest.raises(ValueError, match="immutable published development image"):
        _renderer().render(
            TEMPLATE,
            output,
            f"ghcr.io/carstvaartjes/vonk-forge-api:dev@sha256:{DIGEST}",
            WORKER_IMAGE,
            HERMES_IMAGE,
            channel="dev",
        )
