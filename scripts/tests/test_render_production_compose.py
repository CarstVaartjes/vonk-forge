from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/render-production-compose"
DEV_SCRIPT = ROOT / "scripts/render-dev-compose"
TEMPLATE = ROOT / "deploy/compose/compose.yaml"
DIGEST = "a" * 64
API_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-api:v1.2.3@sha256:{DIGEST}"
WORKER_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-worker:v1.2.3@sha256:{DIGEST}"
HERMES_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-hermes:v1.2.3@sha256:{DIGEST}"
LITELLM_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-litellm:v1.2.3@sha256:{DIGEST}"
DEV_API_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-{'b' * 40}@sha256:{DIGEST}"
DEV_WORKER_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-worker:dev-sha-{'b' * 40}@sha256:{DIGEST}"
DEV_HERMES_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-hermes:dev-sha-{'b' * 40}@sha256:{DIGEST}"
DEV_LITELLM_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-litellm:dev-sha-{'b' * 40}@sha256:{DIGEST}"


def _run_production(
    template: Path,
    output: Path,
    api_image: str = API_IMAGE,
    worker_image: str = WORKER_IMAGE,
    hermes_image: str = HERMES_IMAGE,
    litellm_image: str = LITELLM_IMAGE,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--template",
            str(template),
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
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _replace(value: object, replacements: dict[str, str]) -> object:
    if isinstance(value, str):
        return replacements.get(value, value)
    if isinstance(value, list):
        return [_replace(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, replacements) for key, item in value.items()}
    return value


def test_production_and_development_render_the_same_resolved_runtime_model(
    tmp_path: Path,
) -> None:
    """Catches a renderer that leaves production on a different Compose graph."""
    production = tmp_path / "production" / "docker-compose.yaml"
    development = tmp_path / "development" / "docker-compose.yaml"
    production.parent.mkdir()
    development.parent.mkdir()

    production_result = _run_production(TEMPLATE, production)
    development_result = subprocess.run(
        [
            sys.executable,
            str(DEV_SCRIPT),
            "--template",
            str(TEMPLATE),
            "--output",
            str(development),
            "--api-image",
            DEV_API_IMAGE,
            "--worker-image",
            DEV_WORKER_IMAGE,
            "--hermes-image",
            DEV_HERMES_IMAGE,
            "--litellm-image",
            DEV_LITELLM_IMAGE,
            "--channel",
            "dev",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert production_result.returncode == 0, production_result.stderr
    assert development_result.returncode == 0, development_result.stderr

    production_model = yaml.safe_load(production.read_text(encoding="utf-8"))
    development_model = yaml.safe_load(development.read_text(encoding="utf-8"))
    development_model = _replace(
        development_model,
        {
            DEV_API_IMAGE: API_IMAGE,
            DEV_WORKER_IMAGE: WORKER_IMAGE,
            DEV_HERMES_IMAGE: HERMES_IMAGE,
            DEV_LITELLM_IMAGE: LITELLM_IMAGE,
        },
    )

    assert production_model == development_model
    assert "include:" not in production.read_text(encoding="utf-8")


def test_render_replaces_every_control_image_without_resolving_operator_inputs(
    tmp_path: Path,
) -> None:
    output = tmp_path / "docker-compose.production.yml"

    result = _run_production(TEMPLATE, output)

    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert document["services"]["control-api"]["image"] == API_IMAGE
    assert document["services"]["control-worker"]["image"] == WORKER_IMAGE
    assert document["services"]["hermes-agent"]["image"] == HERMES_IMAGE
    assert document["services"]["litellm"]["image"] == LITELLM_IMAGE
    assert all(
        isinstance(service, dict)
        and isinstance(service.get("image"), str)
        and "@sha256:" in service["image"]
        and "${" not in service["image"]
        for service in document["services"].values()
    )
    assert "CONTROL_API_IMAGE" not in text
    assert "CONTROL_WORKER_IMAGE" not in text
    assert "${NAS_LAN_IP:?set reserved NAS LAN IP}" in text
    assert "VONK_DEPLOYMENT_MODE: production" in text
    assert {path.name for path in tmp_path.iterdir()} == {
        "docker-compose.production.yml"
    }


@pytest.mark.parametrize(
    "image",
    (
        "ghcr.io/carstvaartjes/vonk-forge-api:latest",
        f"ghcr.io/carstvaartjes/vonk-forge-api:latest@sha256:{DIGEST}",
        f"ghcr.io/carstvaartjes/vonk-forge-api:dev@sha256:{DIGEST}",
        f"ghcr.io/example/vonk-forge-api:latest@sha256:{DIGEST}",
    ),
)
def test_render_rejects_nonproduction_or_unpinned_images(
    tmp_path: Path, image: str
) -> None:
    output = tmp_path / "docker-compose.production.yml"
    output.write_text("preserve\n", encoding="utf-8")

    result = _run_production(TEMPLATE, output, api_image=image)

    assert result.returncode != 0
    assert "immutable production version" in result.stderr
    assert output.read_text(encoding="utf-8") == "preserve\n"


def test_render_rejects_template_token_drift(tmp_path: Path) -> None:
    template = tmp_path / "compose.yaml"
    template.write_text(
        TEMPLATE.read_text(encoding="utf-8").replace(
            "${CONTROL_API_IMAGE:?set a digest-pinned control-api image}",
            "unexpected",
            1,
        ),
        encoding="utf-8",
    )

    result = _run_production(
        template,
        tmp_path / "docker-compose.production.yml",
    )

    assert result.returncode != 0
    assert "API image token" in result.stderr


def test_cli_accepts_a_relative_template_path(tmp_path: Path) -> None:
    output = tmp_path / "docker-compose.yaml"

    result = _run_production(
        TEMPLATE.relative_to(ROOT),
        output,
    )

    assert result.returncode == 0, result.stderr
    assert yaml.safe_load(output.read_text(encoding="utf-8"))["services"]
