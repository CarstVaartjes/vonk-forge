from __future__ import annotations

import os
import subprocess
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


def test_development_bundle_selects_only_the_canonical_graph() -> None:
    document = yaml.safe_load(DEVELOPMENT_COMPOSE.read_text(encoding="utf-8"))
    assert document["include"] == ["compose.yaml"]


def test_development_launcher_invokes_the_canonical_graph_with_published_images(
    tmp_path: Path,
) -> None:
    """Catches a launcher that selects a development-only topology or image build."""
    capture_arguments = tmp_path / "arguments"
    capture_images = tmp_path / "images"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$VONK_CAPTURE_ARGUMENTS\"\n"
        "printf '%s\\n%s\\n' \"$CONTROL_API_IMAGE\" \"$CONTROL_WORKER_IMAGE\" "
        '> "$VONK_CAPTURE_IMAGES"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    api_image = "ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-" + "a" * 40 + "@sha256:" + "b" * 64
    worker_image = "ghcr.io/carstvaartjes/vonk-forge-worker:dev-sha-" + "a" * 40 + "@sha256:" + "c" * 64
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "VONK_CAPTURE_ARGUMENTS": str(capture_arguments),
        "VONK_CAPTURE_IMAGES": str(capture_images),
        "VONK_DEPLOYMENT_ENV_FILE": str(tmp_path / "missing.env"),
        "VONK_DEV_API_IMAGE": api_image,
        "VONK_DEV_WORKER_IMAGE": worker_image,
    }

    subprocess.run(
        [str(DEVELOPMENT_WRAPPER), "config"],
        cwd=ROOT,
        env=environment,
        check=True,
    )

    assert capture_arguments.read_text(encoding="utf-8").splitlines() == [
        "compose",
        "--project-directory",
        str(ROOT),
        "--file",
        str(PRODUCTION_COMPOSE),
        "config",
    ]
    assert capture_images.read_text(encoding="utf-8").splitlines() == [
        api_image,
        worker_image,
    ]

    workflow = DEVELOPMENT_WORKFLOW.read_text(encoding="utf-8")
    assert workflow.count("--template deploy/compose/compose.dev.images.yaml") == 2
    assert workflow.count('--hermes-image "$hermes_image"') == 2


def test_hermes_is_opt_in_in_the_shared_production_graph() -> None:
    document = yaml.safe_load(HERMES_COMPOSE.read_text(encoding="utf-8"))
    assert document["services"]["hermes-agent"]["profiles"] == ["hermes"]
    assert document["x-hermes-service"]["image"] == (
        "${HERMES_AGENT_IMAGE:?set a digest-pinned Hermes image}"
    )
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
    assert "/srv/vonk-forge" not in runbook
    assert "HERMES_DATA_ROOT" not in runbook
    assert "already contains the immutable Hermes image" in runbook


def test_development_image_workflow_validates_the_canonical_graph_with_test_inputs() -> None:
    source = DEVELOPMENT_WORKFLOW.read_text(encoding="utf-8")

    assert "--env-file deploy/compose/tests/test.env" in source
    assert "test_dev_complete_stack.py" not in source
