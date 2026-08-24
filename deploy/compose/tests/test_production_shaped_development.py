from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_COMPOSE = ROOT / "deploy/compose/compose.yaml"
HERMES_COMPOSE = ROOT / "deploy/compose/hermes-agent/compose.yaml"
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
    assert workflow.count('--hermes-image "$hermes_image"') == 2


def test_hermes_is_opt_in_in_the_shared_production_graph() -> None:
    document = yaml.safe_load(HERMES_COMPOSE.read_text(encoding="utf-8"))
    assert document["services"]["hermes-agent"]["profiles"] == ["hermes"]
    assert document["services"]["hermes-litellm-key-provisioner"]["profiles"] == [
        "hermes"
    ]
    assert document["x-hermes-service"]["image"] == (
        "${HERMES_AGENT_IMAGE:?set a digest-pinned Hermes image}"
    )
    tailscale = yaml.safe_load(
        (ROOT / "deploy/compose/tailscale/compose.yaml").read_text(encoding="utf-8")
    )
    configurator = tailscale["services"]["tailscale-configurator"]
    assert "hermes-agent" not in configurator["depends_on"]
    assert configurator["secrets"] == ["hermes-api-key"]
    hermes_text = HERMES_COMPOSE.read_text(encoding="utf-8")
    assert "HERMES_DASHBOARD_ORIGIN:?" not in hermes_text
    assert "HERMES_DATA_ROOT:?" not in hermes_text
    assert "HERMES_API_KEY_FILE:?" not in hermes_text


def test_development_image_workflow_validates_the_canonical_graph_with_test_inputs() -> (
    None
):
    source = DEVELOPMENT_WORKFLOW.read_text(encoding="utf-8")

    assert "--env-file deploy/compose/tests/test.env" in source
    assert "test_dev_complete_stack.py" not in source
