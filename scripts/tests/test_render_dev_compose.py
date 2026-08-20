from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy/compose/compose.yaml"
DEVELOPMENT_TEMPLATE = ROOT / "deploy/compose/compose.dev.images.yaml"
DIGEST = "a" * 64
API_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-{'a' * 40}@sha256:{DIGEST}"
WORKER_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-worker:dev-sha-{'a' * 40}@sha256:{DIGEST}"
HERMES_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-hermes:dev-sha-{'a' * 40}@sha256:{DIGEST}"


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
    assert text.count(API_IMAGE) >= 2
    assert text.count(WORKER_IMAGE) >= 5
    assert set(path.name for path in tmp_path.iterdir()) == {"docker-compose.yaml"}
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


def test_render_accepts_development_template_and_inlines_step_ca(tmp_path: Path) -> None:
    output = tmp_path / "docker-compose.yaml"

    _renderer().render(
        DEVELOPMENT_TEMPLATE,
        output,
        API_IMAGE,
        WORKER_IMAGE,
        HERMES_IMAGE,
        channel="pinned",
    )

    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert document["services"]["control-api"]["environment"]["VONK_AGENT_CA_PROVIDER"] == "step-ca"
    assert "step-ca" in document["services"]
    bootstrap_secrets = document["services"]["control-bootstrap"]["secrets"]
    assert "admin-grant-private-key" in bootstrap_secrets
    assert "step-ca-password" in bootstrap_secrets


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
