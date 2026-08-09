from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "deploy/compose/compose.dev.yaml"
SCRIPT = ROOT / "scripts/dev-compose"
EXPECTED_COMMIT = "a" * 40


def _rendered(tmp_path: Path) -> dict[str, object]:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name, value in {
        "database-url": "postgresql+psycopg://control:dev@postgres:5432/control\n",
        "postgres-password": "dev\n",
        "git-signing-key": "development-key\n",
    }.items():
        (secrets / name).write_text(value, encoding="utf-8")
    origin = tmp_path / "origin.git"
    origin.mkdir()
    environment = os.environ | {
        "VONK_DEV_EXPECTED_COMMIT": EXPECTED_COMMIT,
        "VONK_DEV_ORIGIN_DIR": str(origin),
        "VONK_DEV_SECRETS_DIR": str(secrets),
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(result.stdout)


def test_dev_compose_builds_local_control_services_without_release_images(
    tmp_path: Path,
) -> None:
    rendered = _rendered(tmp_path)
    services = rendered["services"]

    assert services["control-api"]["build"]["target"] == "api"
    assert services["control-worker"]["build"]["target"] == "worker"
    assert services["migrate"]["build"]["target"] == "api"
    assert "image" not in services["control-api"]
    assert "image" not in services["control-worker"]
    assert services["control-api"]["environment"]["VONK_DEPLOYMENT_MODE"] == (
        "development"
    )
    assert services["control-api"]["environment"]["VONK_AGENT_RUNTIME"] == (
        "disabled"
    )
    assert services["control-api"]["ports"] == [
        {
            "host_ip": "127.0.0.1",
            "mode": "ingress",
            "published": "8080",
            "protocol": "tcp",
            "target": 8000,
        }
    ]


def test_dev_compose_initializes_identity_before_api_and_worker(tmp_path: Path) -> None:
    services = _rendered(tmp_path)["services"]

    assert "dev-init" in services
    assert services["dev-init"]["user"] == "0:0"
    assert services["control-api"]["depends_on"]["dev-init"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["control-worker"]["depends_on"]["dev-init"]["condition"] == (
        "service_completed_successfully"
    )


def _volumes_by_target(service: dict[str, object]) -> dict[str, dict[str, object]]:
    return {volume["target"]: volume for volume in service.get("volumes", [])}


def test_dev_compose_runs_packaged_initializer_with_disjoint_runtime_authority(
    tmp_path: Path,
) -> None:
    rendered = _rendered(tmp_path)
    services = rendered["services"]
    initializer = services["dev-init"]

    assert initializer["build"]["target"] == "api"
    assert initializer["command"] == ["python", "-m", "vonk_control.dev_init"]
    assert initializer["environment"] == {
        "VONK_CONTROL_IDENTITY_ROOT": "/control-identity",
        "VONK_DEV_API_SECRET_ROOT": "/api-secrets",
        "VONK_DEV_EXPECTED_COMMIT": EXPECTED_COMMIT,
        "VONK_DEV_LOCAL_ACCEPTANCE": "1",
        "VONK_DEV_REPOSITORY_URL": "file:///source-origin",
        "VONK_DEV_SECRET_SOURCE_ROOT": "/host-secrets",
        "VONK_DEV_WORKER_SECRET_ROOT": "/worker-secrets",
        "VONK_REPOSITORY_PATH": "/repository",
        "VONK_ROUTE_ROOT": "/routes",
        "VONK_STATE_PATH": "/state",
        "VONK_SUPERVISOR_ROOT": "/supervisor",
    }
    init_volumes = _volumes_by_target(initializer)
    assert init_volumes["/source-origin"]["type"] == "bind"
    assert init_volumes["/source-origin"]["read_only"] is True
    assert init_volumes["/repository"]["type"] == "volume"
    assert init_volumes["/api-secrets"]["type"] == "volume"
    assert init_volumes["/worker-secrets"]["type"] == "volume"

    api_volumes = _volumes_by_target(services["control-api"])
    worker_volumes = _volumes_by_target(services["control-worker"])
    assert api_volumes["/repository"]["type"] == "volume"
    assert api_volumes["/repository"].get("read_only", False) is False
    assert worker_volumes["/repository"]["type"] == "volume"
    assert worker_volumes["/repository"]["read_only"] is True
    assert api_volumes["/run/secrets"]["source"].endswith("dev-api-secrets")
    assert worker_volumes["/run/secrets"]["source"].endswith(
        "dev-worker-secrets"
    )
    assert api_volumes["/run/secrets"]["source"] != worker_volumes["/run/secrets"][
        "source"
    ]
    assert services["control-api"]["environment"]["VONK_DEPLOYMENT_BRANCH"] == "main"
    assert services["control-worker"]["environment"]["VONK_DEPLOYMENT_BRANCH"] == (
        "main"
    )
    assert {secret["source"] for secret in services["migrate"]["secrets"]} == {
        "database-url"
    }


def test_dev_compose_script_publishes_head_to_a_local_acceptance_origin(
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in ("postgres-password", "database-url", "git-signing-key"):
        (secrets / name).write_text("already-created\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "printf '%s\\n%s\\n%s\\n' \"${VONK_DEV_EXPECTED_COMMIT:-}\" "
        "\"${VONK_DEV_ORIGIN_DIR:-}\" \"$VONK_DEV_SECRETS_DIR\" "
        "> \"$VONK_TEST_CAPTURE_DIR/environment\"\n"
        "printf '%s\\n' \"$@\" > \"$VONK_TEST_CAPTURE_DIR/arguments\"\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    capture = tmp_path / "capture"
    capture.mkdir()
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_DEV_ORIGIN_DIR": str(tmp_path / "origin.git"),
        "VONK_DEV_SECRETS_DIR": str(secrets),
        "VONK_TEST_CAPTURE_DIR": str(capture),
    }

    subprocess.run(
        (str(SCRIPT), "config"),
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    expected = subprocess.run(
        ("git", "-C", str(ROOT), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    exported_commit, origin, exported_secrets = (
        capture / "environment"
    ).read_text(encoding="utf-8").splitlines()
    assert exported_commit == expected
    assert origin == str(tmp_path / "origin.git")
    assert exported_secrets == str(secrets)
    assert subprocess.run(
        ("git", "--git-dir", origin, "rev-parse", "refs/heads/main"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == expected
    arguments = (capture / "arguments").read_text(encoding="utf-8").splitlines()
    assert arguments[-1] == "config"
    assert str(COMPOSE) in arguments
