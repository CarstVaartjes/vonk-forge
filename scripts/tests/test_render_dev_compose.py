from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy/compose/compose.yaml"
DIGEST = "a" * 64
API_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-api:dev@sha256:{DIGEST}"
WORKER_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-worker:dev@sha256:{DIGEST}"


def _renderer():
    script = ROOT / "scripts/render-dev-compose"
    loader = importlib.machinery.SourceFileLoader("render_dev_compose", str(script))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_render_inlines_compose_includes(tmp_path: Path) -> None:
    output = tmp_path / "docker-compose.yaml"

    _renderer().render(TEMPLATE, output, API_IMAGE, WORKER_IMAGE, channel="pinned")

    text = output.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert "\ninclude:" not in text
    assert "tailscale-gateway" in document["services"]
    assert "hermes-agent" in document["services"]
    assert document["services"]["hermes-agent"]["profiles"] == ["hermes"]
    assert text.count(API_IMAGE) >= 2
    assert text.count(WORKER_IMAGE) >= 6
    assert (tmp_path / "Caddyfile").is_file()
    assert (tmp_path / "tailscale/configure.sh").is_file()
    assert "./tailscale/configure.sh:/usr/local/bin/configure-tailscale:ro" in text
    assert not (tmp_path / "hermes-agent").exists()
    assert not (tmp_path / "step-ca").exists()
    assert not (tmp_path / "bin").exists()
    assert not (tmp_path / "trust").exists()
