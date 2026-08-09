from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/render-dev-compose"
TEMPLATE = ROOT / "deploy/compose/compose.dev.images.yaml"
COMMIT = "a" * 40
DIGEST = "b" * 64
API_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-{COMMIT}@sha256:{DIGEST}"
WORKER_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-worker:dev-sha-{COMMIT}@sha256:{DIGEST}"


def _renderer():
    loader = importlib.machinery.SourceFileLoader("render_dev_compose", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _rendered(tmp_path: Path) -> tuple[object, Path]:
    output = tmp_path / "docker-compose.yml"
    renderer = _renderer()
    renderer.render(TEMPLATE, output, API_IMAGE, WORKER_IMAGE, COMMIT)
    return renderer, output


def test_render_accepts_exact_public_dev_images_and_replaces_each_token_once(
    tmp_path: Path,
) -> None:
    _, output = _rendered(tmp_path)

    text = output.read_text(encoding="utf-8")
    assert text.count(API_IMAGE) == 1
    assert text.count(WORKER_IMAGE) == 1
    assert text.count(f"VONK_DEV_EXPECTED_COMMIT: {COMMIT}") == 1
    assert "__VONK_" not in text


@pytest.mark.parametrize(
    ("api_image", "worker_image", "commit"),
    [
        ("ghcr.io/carstvaartjes/vonk-forge-api:latest@sha256:" + DIGEST, WORKER_IMAGE, COMMIT),
        ("ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-" + COMMIT, WORKER_IMAGE, COMMIT),
        ("ghcr.io/carstvaartjes/vonk-forge-api:v1.2.3@sha256:" + DIGEST, WORKER_IMAGE, COMMIT),
        ("ghcr.io/example/vonk-forge-api:dev-sha-" + COMMIT + "@sha256:" + DIGEST, WORKER_IMAGE, COMMIT),
        (API_IMAGE.replace(COMMIT, "c" * 40), WORKER_IMAGE, COMMIT),
        (API_IMAGE, WORKER_IMAGE, "A" * 40),
    ],
)
def test_render_rejects_invalid_inputs_without_replacing_output(
    tmp_path: Path, api_image: str, worker_image: str, commit: str
) -> None:
    output = tmp_path / "docker-compose.yml"
    output.write_text("previous output\n", encoding="utf-8")

    with pytest.raises(ValueError):
        _renderer().render(TEMPLATE, output, api_image, worker_image, commit)

    assert output.read_text(encoding="utf-8") == "previous output\n"
    assert list(tmp_path.glob(".render-dev-compose-*")) == []


def test_render_rejects_source_output_identity_and_unresolved_template_tokens(
    tmp_path: Path,
) -> None:
    renderer = _renderer()
    with pytest.raises(ValueError):
        renderer.render(TEMPLATE, TEMPLATE, API_IMAGE, WORKER_IMAGE, COMMIT)

    template = tmp_path / "template.yaml"
    template.write_text(
        "services: {}\n"
        "api: __VONK_API_IMAGE__\n"
        "worker: __VONK_WORKER_IMAGE__\n"
        "commit: __VONK_EXPECTED_COMMIT__\n"
        "value: __VONK_UNKNOWN__\n",
        encoding="utf-8",
    )
    output = tmp_path / "docker-compose.yml"
    output.write_text("previous output\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unresolved"):
        renderer.render(template, output, API_IMAGE, WORKER_IMAGE, COMMIT)

    assert output.read_text(encoding="utf-8") == "previous output\n"


def test_render_preserves_output_and_removes_staging_when_compose_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    renderer = _renderer()
    output = tmp_path / "docker-compose.yml"
    output.write_text("previous output\n", encoding="utf-8")

    def fail_validation(stage: Path) -> None:
        assert (stage.parent / "secrets" / "postgres-password").is_file()
        raise subprocess.CalledProcessError(1, ["docker", "compose", "config", "-q"])

    monkeypatch.setattr(renderer, "_validate_compose", fail_validation)

    with pytest.raises(subprocess.CalledProcessError):
        renderer.render(TEMPLATE, output, API_IMAGE, WORKER_IMAGE, COMMIT)

    assert output.read_text(encoding="utf-8") == "previous output\n"
    assert list(tmp_path.glob(".render-dev-compose-*")) == []


def test_rendered_compose_is_image_only_and_has_exact_runtime_boundaries(
    tmp_path: Path,
) -> None:
    _, output = _rendered(tmp_path)
    rendered = json.loads(
        subprocess.run(
            ("docker", "compose", "-f", str(output), "config", "--format", "json"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    services = rendered["services"]

    assert all("build" not in service for service in services.values())
    assert services["dev-init"]["image"] == API_IMAGE
    assert services["migrate"]["image"] == API_IMAGE
    assert services["control-api"]["image"] == API_IMAGE
    assert services["control-worker"]["image"] == WORKER_IMAGE
    assert services["dev-init"]["environment"]["VONK_DEV_EXPECTED_COMMIT"] == COMMIT
    assert services["control-api"]["environment"]["VONK_DEPLOYMENT_BRANCH"] == "main"
    assert services["control-worker"]["environment"]["VONK_DEPLOYMENT_BRANCH"] == "main"

    volumes = {
        service_name: {volume["target"]: volume for volume in service.get("volumes", [])}
        for service_name, service in services.items()
    }
    assert volumes["control-api"]["/repository"].get("read_only", False) is False
    assert volumes["control-worker"]["/repository"]["read_only"] is True
    assert all(
        volume.get("type") != "bind" or volume["target"] != "/repository"
        for service in services.values()
        for volume in service.get("volumes", [])
    )
    assert {secret["source"] for secret in services["postgres"]["secrets"]} == {"postgres-password"}
    assert {secret["source"] for secret in services["dev-init"]["secrets"]} == {"database-url", "git-signing-key"}
    assert {secret["source"] for secret in services["migrate"]["secrets"]} == {"database-url"}
    assert "secrets" not in services["control-api"]
    assert "secrets" not in services["control-worker"]
    assert volumes["control-api"]["/run/secrets"]["source"].endswith("dev-api-secrets")
    assert volumes["control-worker"]["/run/secrets"]["source"].endswith("dev-worker-secrets")


def test_template_is_development_only_and_never_mentions_local_acceptance() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "VONK_DEV_LOCAL_ACCEPTANCE" not in text
    assert "file:///" not in text
    assert "build:" not in text
    assert "context:" not in text
    assert "../" not in text
    assert text.count("${VONK_DEV_PORT:-8080}") == 1
