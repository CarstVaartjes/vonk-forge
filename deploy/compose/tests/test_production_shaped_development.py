from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_COMPOSE = ROOT / "deploy/compose/compose.yaml"
DEVELOPMENT_COMPOSE = ROOT / "deploy/compose/compose.dev.images.yaml"
DEVELOPMENT_WRAPPER = ROOT / "scripts/dev-compose"


def _services(path: Path, *, _seen: set[Path] | None = None) -> set[str]:
    seen = set() if _seen is None else _seen
    path = path.resolve()
    if path in seen:
        return set()
    seen.add(path)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    services = set(document.get("services", {}))
    for included in document.get("include", []):
        included_path = included if isinstance(included, str) else included["path"]
        services.update(_services(path.parent / included_path, _seen=seen))
    return services


def test_development_uses_the_production_runtime_service_graph() -> None:
    assert _services(DEVELOPMENT_COMPOSE) == _services(PRODUCTION_COMPOSE)


def test_development_wrapper_selects_published_images_only() -> None:
    source = DEVELOPMENT_WRAPPER.read_text(encoding="utf-8")

    assert "dev-local" not in source
    assert "source-origin" not in source
    assert "git clone" not in source
    assert "build:" not in source
    assert "ghcr.io/carstvaartjes/vonk-forge-api" in source
    assert "ghcr.io/carstvaartjes/vonk-forge-worker" in source
