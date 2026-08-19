from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_COMPOSE = ROOT / "deploy/compose/compose.yaml"
HERMES_COMPOSE = ROOT / "deploy/compose/hermes-agent/compose.yaml"
DEVELOPMENT_COMPOSE = ROOT / "deploy/compose/compose.dev.images.yaml"
DEVELOPMENT_WRAPPER = ROOT / "scripts/dev-compose"
DEVELOPMENT_WORKFLOW = ROOT / ".github/workflows/dev-images.yml"


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


def test_development_bundle_selects_the_builtin_agent_ca_overlay() -> None:
    document = yaml.safe_load(DEVELOPMENT_COMPOSE.read_text(encoding="utf-8"))
    assert document["include"] == ["compose.yaml", "compose.builtin-ca.yaml"]


def test_development_wrapper_selects_published_images_only() -> None:
    source = DEVELOPMENT_WRAPPER.read_text(encoding="utf-8")

    assert "dev-local" not in source
    assert "source-origin" not in source
    assert "git clone" not in source
    assert "build:" not in source
    assert "ghcr.io/carstvaartjes/vonk-forge-api" in source
    assert "ghcr.io/carstvaartjes/vonk-forge-worker" in source

    workflow = DEVELOPMENT_WORKFLOW.read_text(encoding="utf-8")
    assert workflow.count("--template deploy/compose/compose.dev.images.yaml") == 2


def test_hermes_is_opt_in_in_the_shared_production_graph() -> None:
    document = yaml.safe_load(HERMES_COMPOSE.read_text(encoding="utf-8"))
    assert document["services"]["hermes-agent"]["profiles"] == ["hermes"]
    assert "profile-required" in document["x-hermes-service"]["image"]
    tailscale = yaml.safe_load(
        (ROOT / "deploy/compose/tailscale/compose.yaml").read_text(encoding="utf-8")
    )
    assert "hermes-agent" not in tailscale["services"]["tailscale-configurator"]["depends_on"]
    hermes_text = HERMES_COMPOSE.read_text(encoding="utf-8")
    assert "HERMES_DASHBOARD_ORIGIN:?" not in hermes_text
    assert "HERMES_DATA_ROOT:?" not in hermes_text
    assert "HERMES_API_KEY_FILE:?" not in hermes_text

    runbook = (ROOT / "docs/runbooks/development-nas-installation.md").read_text(
        encoding="utf-8"
    )
    assert "--profile hermes up -d --wait" in runbook


def test_development_image_workflow_validates_the_canonical_graph_with_test_inputs() -> None:
    source = DEVELOPMENT_WORKFLOW.read_text(encoding="utf-8")

    assert "--env-file deploy/compose/tests/test.env" in source
    assert "test_dev_complete_stack.py" not in source
