from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy/compose/compose.yaml"
DIGEST = "a" * 64
API_IMAGE = f"ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-{'a' * 40}@sha256:{DIGEST}"
WORKER_IMAGE = (
    f"ghcr.io/carstvaartjes/vonk-forge-worker:dev-sha-{'a' * 40}@sha256:{DIGEST}"
)
HERMES_IMAGE = (
    f"ghcr.io/carstvaartjes/vonk-forge-hermes:dev-sha-{'a' * 40}@sha256:{DIGEST}"
)
LITELLM_IMAGE = (
    f"ghcr.io/carstvaartjes/vonk-forge-litellm:dev-sha-{'a' * 40}@sha256:{DIGEST}"
)
DEV_API_IMAGE = "ghcr.io/carstvaartjes/vonk-forge-api:dev"
DEV_WORKER_IMAGE = "ghcr.io/carstvaartjes/vonk-forge-worker:dev"


SCRIPT = ROOT / "scripts/render-dev-compose"


def _renderer_module() -> Any:
    loader = importlib.machinery.SourceFileLoader("render_dev_compose", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _run_renderer(
    output: Path,
    *,
    api_image: str = API_IMAGE,
    worker_image: str = WORKER_IMAGE,
    hermes_image: str = HERMES_IMAGE,
    litellm_image: str = LITELLM_IMAGE,
    channel: str = "pinned",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--template",
            str(TEMPLATE),
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
            "--channel",
            channel,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_render_produces_a_single_self_contained_compose_file(
    tmp_path: Path,
) -> None:
    """Catches a deployment bundle that needs files beside docker-compose.yaml."""
    output = tmp_path / "docker-compose.yaml"

    result = _run_renderer(output)

    assert result.returncode == 0, result.stderr
    text = output.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert "\ninclude:" not in text
    assert "tailscale-gateway" in document["services"]
    assert "hermes-agent" in document["services"]
    assert document["services"]["hermes-agent"]["profiles"] == ["hermes"]
    assert document["services"]["hermes-litellm-key-provisioner"]["profiles"] == [
        "hermes"
    ]
    assert document["services"]["control-api"]["image"] == API_IMAGE
    assert document["services"]["control-worker"]["image"] == WORKER_IMAGE
    assert document["services"]["litellm"]["image"] == LITELLM_IMAGE
    assert {path.name for path in tmp_path.iterdir()} == {"docker-compose.yaml"}
    # Runtime configuration ships in the Controller image instead.
    assert "configs" not in document
    assert not any(
        isinstance(volume, str) and volume.startswith("./")
        for service in document["services"].values()
        for volume in service.get("volumes", [])
    )
    images = [service["image"] for service in document["services"].values()]
    # Every image is a literal: Vonk images carry their digest and third-party
    # images an explicit version tag.
    assert all("${" not in image for image in images)
    assert all(
        "@sha256:" in image
        for image in images
        if image.startswith("ghcr.io/carstvaartjes/vonk-forge-")
    )
    assert all(
        ":" in image.rsplit("/", 1)[-1] and not image.endswith(":latest")
        for image in images
    )

    for profile in ([], ["--profile", "hermes"]):
        config = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(ROOT / "deploy/compose/tests/test.env"),
                "-f",
                str(output),
                *profile,
                "config",
                "-q",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert config.returncode == 0, config.stderr


# Starts a real PostgreSQL container and runs initdb (~8-12 s on CI runners).
@pytest.mark.slow(30)
@pytest.mark.lane
def test_rendered_postgres_starts_with_an_inert_initializer(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        if os.getenv("CI"):
            raise AssertionError("Docker is unavailable")
        pytest.skip("Docker is required for the rendered PostgreSQL config test")
    docker_info = subprocess.run(
        ["docker", "info"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if docker_info.returncode != 0:
        if os.getenv("CI"):
            raise AssertionError("Docker is unavailable")
        pytest.skip("Docker is unavailable for the rendered PostgreSQL config test")

    rendered = tmp_path / "rendered.yaml"
    result = _run_renderer(rendered)
    assert result.returncode == 0, result.stderr
    document = yaml.safe_load(rendered.read_text(encoding="utf-8"))
    postgres = document["services"]["postgres"]
    backups = tmp_path / "backups"
    backups.mkdir()
    # tmpfs data: initdb's fsyncs dominate the test on a Docker volume. The
    # Controller normally stages the PostgreSQL assets; bind the sources here.
    postgres["volumes"] = [
        {"type": "tmpfs", "target": "/var/lib/postgresql"},
        {"type": "bind", "source": str(backups), "target": "/backups"},
        {
            "type": "bind",
            "source": str(ROOT / "deploy/compose/postgres"),
            "target": "/run/vonk-runtime-assets/postgres",
            "read_only": True,
        },
    ]
    postgres.pop("networks")
    postgres["healthcheck"] = {
        "test": [
            "CMD-SHELL",
            (
                "psql -U control -d control -tAc "
                "\"SELECT count(*) FROM pg_database WHERE datname = 'litellm'\""
                " | grep -qx 1"
            ),
        ],
        "interval": "250ms",
        "timeout": "5s",
        "retries": 240,
    }
    postgres_password = tmp_path / "postgres-password"
    postgres_password.write_text("postgres-password\n", encoding="ascii")
    litellm_password = tmp_path / "litellm-password"
    litellm_password.write_text("c" * 64 + "\n", encoding="ascii")
    compose = {
        "services": {"postgres": postgres},
        "secrets": {
            "postgres-password": {"file": str(postgres_password)},
            "litellm-database-password": {"file": str(litellm_password)},
        },
    }
    rendered.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    project = f"vonk-rendered-postgres-{uuid.uuid4().hex}"
    command = ["docker", "compose", "-p", project, "-f", str(rendered)]
    try:
        # Healthy only once the rendered initializer has created LiteLLM's
        # database; a short interval keeps the wait close to initdb's time.
        subprocess.run(
            [*command, "up", "-d", "--wait", "--wait-timeout", "60", "postgres"],
            check=True,
            capture_output=True,
            text=True,
            timeout=90,
        )
        staged = subprocess.check_output(
            [
                *command,
                "exec",
                "-T",
                "postgres",
                "stat",
                "-c",
                "%F:%u:%g:%a",
                "/docker-entrypoint-initdb.d/10-vonk-forge-databases.sh",
            ],
            text=True,
            timeout=10,
        ).strip()
        assert staged == "regular file:0:0:444"
    finally:
        subprocess.run(
            [*command, "down", "--volumes", "--remove-orphans", "--timeout", "0"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )


def test_render_uses_canonical_template_and_inlines_step_ca(tmp_path: Path) -> None:
    output = tmp_path / "docker-compose.yaml"

    result = _run_renderer(output)

    assert result.returncode == 0, result.stderr
    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert "step-ca" in document["services"]
    api_secrets = document["services"]["control-api"]["secrets"]
    assert "step-ca-password" in api_secrets


def test_render_rejects_the_mutable_development_image_alias(tmp_path: Path) -> None:
    """Catches a development bundle that is not reproducible from its manifest."""
    output = tmp_path / "docker-compose.yaml"

    result = _run_renderer(
        output,
        api_image=f"ghcr.io/carstvaartjes/vonk-forge-api:dev@sha256:{DIGEST}",
        channel="dev",
    )

    assert result.returncode != 0
    assert "immutable published development image" in result.stderr


def test_render_dev_floats_vonk_images_and_keeps_third_party_pins(
    tmp_path: Path,
) -> None:
    output = tmp_path / "docker-compose.yaml"

    result = _run_renderer(
        output,
        api_image=DEV_API_IMAGE,
        worker_image=DEV_WORKER_IMAGE,
        channel="dev",
    )

    assert result.returncode == 0, result.stderr
    document = yaml.safe_load(output.read_text(encoding="utf-8"))
    services = document["services"]
    assert services["control-api"]["image"] == DEV_API_IMAGE
    assert services["control-api"]["pull_policy"] == "always"
    assert services["control-worker"]["image"] == DEV_WORKER_IMAGE
    assert services["control-worker"]["pull_policy"] == "always"
    assert (
        services["hermes-agent"]["image"]
        == "ghcr.io/carstvaartjes/vonk-forge-hermes:dev"
    )
    assert (
        services["litellm"]["image"] == "ghcr.io/carstvaartjes/vonk-forge-litellm:dev"
    )
    lock = json.loads((ROOT / "deploy/compose/images.lock.json").read_text())
    for service in services.values():
        if service["image"].startswith("ghcr.io/carstvaartjes/vonk-forge-"):
            assert service["image"].endswith(":dev")
            assert service["pull_policy"] == "always"
        else:
            # Third-party images keep the exact version the release tested.
            assert service["image"] in lock["images"].values()


def test_render_dev_rejects_role_swapped_mutable_aliases(tmp_path: Path) -> None:
    output = tmp_path / "docker-compose.yaml"

    result = _run_renderer(
        output,
        api_image=DEV_WORKER_IMAGE,
        worker_image=DEV_API_IMAGE,
        channel="dev",
    )

    assert result.returncode != 0
    assert "immutable published development image" in result.stderr
