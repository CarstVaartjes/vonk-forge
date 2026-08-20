from __future__ import annotations

import importlib.machinery
import importlib.util
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
COMPOSE_ROOT = ROOT / "deploy/compose"
DEFAULT_SERVICES = {
    "caddy",
    "control-api",
    "control-signer",
    "control-worker",
    "grafana",
    "litellm",
    "postgres",
    "prometheus",
    "registry",
    "step-ca",
    "tailscale-configurator",
    "tailscale-gateway",
}
ALL_SERVICES = DEFAULT_SERVICES | {"hermes-agent"}
PINNED_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}(?:}|$)")


def _canonical_model() -> dict[str, object]:
    script = ROOT / "scripts/render-dev-compose"
    loader = importlib.machinery.SourceFileLoader(
        "fresh_runtime_contract_renderer", str(script)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module._compose_document(COMPOSE_ROOT / "compose.yaml")


def _is_sleep_only(command: object) -> bool:
    if isinstance(command, str):
        return re.fullmatch(r"\s*sleep(?:\s+\S+)?\s*", command) is not None
    return isinstance(command, list) and bool(command) and command[0] == "sleep"


def _dependency_conditions(service: dict[str, object]) -> list[str]:
    dependencies = service.get("depends_on", {})
    if not isinstance(dependencies, dict):
        return []
    return [
        dependency["condition"]
        for dependency in dependencies.values()
        if isinstance(dependency, dict) and isinstance(dependency.get("condition"), str)
    ]


def test_canonical_model_has_the_exact_default_and_optional_service_sets() -> None:
    """Catches an accidental topology fork or an opt-in service that starts by default."""
    model = _canonical_model()
    services = model["services"]
    assert isinstance(services, dict)

    default_services = {
        name
        for name, service in services.items()
        if isinstance(service, dict) and "profiles" not in service
    }
    assert default_services == DEFAULT_SERVICES
    assert set(services) == ALL_SERVICES
    assert services["hermes-agent"]["profiles"] == ["hermes"]
    assert {
        profile
        for service in services.values()
        if isinstance(service, dict)
        for profile in service.get("profiles", [])
    } == {"hermes"}


def test_canonical_model_has_step_ca_without_an_overlay() -> None:
    """Catches a development or production launch that can omit the agent CA."""
    model = _canonical_model()
    services = model["services"]
    assert isinstance(services, dict)

    assert "step-ca" in services
    assert not (COMPOSE_ROOT / "compose.step-ca.yaml").exists()
    assert not (COMPOSE_ROOT / "compose.builtin-ca.yaml").exists()
    development = yaml.safe_load(
        (COMPOSE_ROOT / "compose.dev.images.yaml").read_text(encoding="utf-8")
    )
    assert development == {"include": ["compose.yaml"]}


def test_canonical_model_has_no_one_shot_runtime_service() -> None:
    """Catches a completed helper container or sleep placeholder in the runtime graph."""
    model = _canonical_model()
    services = model["services"]
    assert isinstance(services, dict)

    for name, service in services.items():
        assert isinstance(service, dict), name
        assert service.get("restart") != "no", name
        assert not _is_sleep_only(service.get("command")), name
        assert not _is_sleep_only(service.get("entrypoint")), name
        assert "service_completed_successfully" not in _dependency_conditions(service), name


def test_canonical_model_has_healthchecks_and_digest_locked_images() -> None:
    """Catches a service that can be started without readiness or a reproducible image."""
    model = _canonical_model()
    services = model["services"]
    assert isinstance(services, dict)

    for name, service in services.items():
        assert isinstance(service, dict), name
        assert "healthcheck" in service, name
        image = service.get("image")
        assert isinstance(image, str), name
        if ":?set " in image:
            assert ":?set a digest-pinned " in image, name
        else:
            assert PINNED_DIGEST.search(image), name


def test_site_path_inputs_are_relative_to_the_uploaded_directory() -> None:
    """Catches an installation bundle that only works at one NAS filesystem path."""
    environment = (COMPOSE_ROOT / ".env.example").read_text(encoding="utf-8")

    for line in environment.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.endswith(("_FILE", "_PATH")):
            assert value.startswith("./secrets/"), name


def test_canonical_model_has_no_bootstrap_runtime_dependency() -> None:
    """Catches a helper container being restored to the runtime graph."""
    model = _canonical_model()
    services = model["services"]
    assert isinstance(services, dict)

    assert "control-bootstrap" not in services
    assert all(
        "control-bootstrap" not in service.get("depends_on", {})
        for service in services.values()
        if isinstance(service, dict)
    )
