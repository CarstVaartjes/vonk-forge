from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/render-production-compose"
TEMPLATE = ROOT / "deploy/compose/compose.yaml"
DIGEST = "a" * 64
API_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-api:latest@sha256:{DIGEST}"
WORKER_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-worker:latest@sha256:{DIGEST}"


def _renderer():
    loader = importlib.machinery.SourceFileLoader(
        "render_production_compose", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_render_replaces_every_control_image_without_resolving_operator_inputs(
    tmp_path: Path,
) -> None:
    output = tmp_path / "docker-compose.production.yml"

    _renderer().render(TEMPLATE, output, API_IMAGE, WORKER_IMAGE)

    text = output.read_text(encoding="utf-8")
    assert text.count(API_IMAGE) == 2
    assert text.count(WORKER_IMAGE) == 6
    assert "CONTROL_API_IMAGE" not in text
    assert "CONTROL_WORKER_IMAGE" not in text
    assert "${NAS_LAN_IP:?set reserved NAS LAN IP}" in text
    assert "VONK_DEPLOYMENT_MODE: production" in text


@pytest.mark.parametrize(
    "image",
    (
        "ghcr.io/carstvaartjes/vonk-forge-api:latest",
        f"ghcr.io/carstvaartjes/vonk-forge-api:dev@sha256:{DIGEST}",
        f"ghcr.io/example/vonk-forge-api:latest@sha256:{DIGEST}",
    ),
)
def test_render_rejects_nonproduction_or_unpinned_images(
    tmp_path: Path, image: str
) -> None:
    output = tmp_path / "docker-compose.production.yml"
    output.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(ValueError):
        _renderer().render(TEMPLATE, output, image, WORKER_IMAGE)

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

    with pytest.raises(ValueError, match="API image token"):
        _renderer().render(
            template,
            tmp_path / "docker-compose.production.yml",
            API_IMAGE,
            WORKER_IMAGE,
        )
