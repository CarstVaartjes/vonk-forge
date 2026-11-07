from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_COMPOSE = ROOT / "deploy/compose/compose.yaml"
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


def test_development_has_no_alternate_runtime_service_graph() -> None:
    assert not (ROOT / "deploy/compose/compose.dev.yaml").exists()
    assert not (ROOT / "deploy/compose/compose.dev.images.yaml").exists()
    assert _services(PRODUCTION_COMPOSE)


def test_development_workflow_renders_only_the_canonical_graph() -> None:
    workflow = DEVELOPMENT_WORKFLOW.read_text(encoding="utf-8")
    assert workflow.count("--template deploy/compose/compose.yaml") == 2


def test_tailscale_configurator_needs_no_secret() -> None:
    tailscale = yaml.safe_load(
        (ROOT / "deploy/compose/tailscale/compose.yaml").read_text(encoding="utf-8")
    )
    configurator = tailscale["services"]["tailscale-configurator"]
    assert "secrets" not in configurator


def test_development_image_workflow_validates_the_canonical_graph_with_test_inputs() -> (
    None
):
    source = DEVELOPMENT_WORKFLOW.read_text(encoding="utf-8")

    assert "--env-file deploy/compose/tests/test.env" in source
    assert "test_dev_complete_stack.py" not in source
