from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

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
    environment = os.environ | {
        "VONK_DEV_EXPECTED_COMMIT": EXPECTED_COMMIT,
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
    assert init_volumes["/source-origin"]["source"] == str(
        ROOT / ".dev/vonk-forge-origin.git"
    )
    assert "/host-secrets" not in init_volumes
    assert init_volumes["/repository"]["type"] == "volume"
    assert init_volumes["/api-secrets"]["type"] == "volume"
    assert init_volumes["/worker-secrets"]["type"] == "volume"
    assert {
        (secret["source"], secret["target"])
        for secret in initializer["secrets"]
    } == {
        ("database-url", "/host-secrets/database-url"),
        ("git-signing-key", "/host-secrets/git-signing-key"),
    }
    assert "postgres-password" not in {
        secret["source"] for secret in initializer["secrets"]
    }

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


def _script_repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    local_script = repository / "scripts/dev-compose"
    local_script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, local_script)
    local_compose = repository / "deploy/compose/compose.dev.yaml"
    local_compose.parent.mkdir(parents=True)
    local_compose.write_text("services: {}\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q", "-b", "main", str(repository)), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.email", "test@example.invalid"),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.name", "Test"),
        check=True,
    )
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-qm", "fixture"), check=True
    )
    return repository, local_script


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "printf '%s\\n%s\\n%s\\n' \"${VONK_DEV_EXPECTED_COMMIT:-}\" "
        "\"${VONK_DEV_ORIGIN_DIR:-absent}\" \"$VONK_DEV_SECRETS_DIR\" "
        "> \"$VONK_TEST_CAPTURE_DIR/environment\"\n"
        "printf '%s\\n' \"$@\" > \"$VONK_TEST_CAPTURE_DIR/arguments\"\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    capture = tmp_path / "capture"
    capture.mkdir()
    return fake_bin, capture


def _existing_secrets(tmp_path: Path) -> Path:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in ("postgres-password", "database-url", "git-signing-key"):
        (secrets / name).write_text("already-created\n", encoding="utf-8")
    return secrets


def test_dev_compose_script_publishes_head_to_its_fixed_owned_origin(
    tmp_path: Path,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin, capture = _fake_docker(tmp_path)
    arbitrary_origin = tmp_path / "arbitrary-origin.git"
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_DEV_ORIGIN_DIR": str(arbitrary_origin),
        "VONK_DEV_SECRETS_DIR": str(secrets),
        "VONK_TEST_CAPTURE_DIR": str(capture),
    }

    subprocess.run(
        (str(local_script), "config"),
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    expected = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    exported_commit, origin, exported_secrets = (
        capture / "environment"
    ).read_text(encoding="utf-8").splitlines()
    assert exported_commit == expected
    assert origin == "absent"
    assert exported_secrets == str(secrets)
    fixed_origin = repository / ".dev/vonk-forge-origin.git"
    assert not arbitrary_origin.exists()
    assert subprocess.run(
        ("git", "--git-dir", str(fixed_origin), "rev-parse", "refs/heads/main"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == expected
    marker = repository / ".dev/.vonk-forge-origin.owner"
    assert marker.read_bytes() == b"vonk-forge-dev-origin-v1\n"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert marker.stat().st_uid == os.geteuid()
    arguments = (capture / "arguments").read_text(encoding="utf-8").splitlines()
    assert arguments[-1] == "config"
    assert str(repository / "deploy/compose/compose.dev.yaml") in arguments


def test_dev_compose_script_rejects_an_unmarked_existing_bare_origin(
    tmp_path: Path,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin, capture = _fake_docker(tmp_path)
    development_root = repository / ".dev"
    development_root.mkdir(mode=0o700)
    subprocess.run(
        ("git", "init", "--bare", "-q", str(development_root / "vonk-forge-origin.git")),
        check=True,
    )
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_DEV_SECRETS_DIR": str(secrets),
        "VONK_TEST_CAPTURE_DIR": str(capture),
    }

    result = subprocess.run(
        (str(local_script), "config"),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "ownership marker" in result.stderr
    assert not (capture / "environment").exists()


@pytest.mark.parametrize("fault", ("content", "mode", "hardlink"))
def test_dev_compose_script_requires_an_exact_safe_origin_marker(
    tmp_path: Path, fault: str
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin, capture = _fake_docker(tmp_path)
    development_root = repository / ".dev"
    development_root.mkdir(mode=0o700)
    origin = development_root / "vonk-forge-origin.git"
    subprocess.run(("git", "init", "--bare", "-q", str(origin)), check=True)
    origin.chmod(0o700)
    marker = development_root / ".vonk-forge-origin.owner"
    marker.write_bytes(
        b"wrong-owner\n" if fault == "content" else b"vonk-forge-dev-origin-v1\n"
    )
    marker.chmod(0o644 if fault == "mode" else 0o600)
    if fault == "hardlink":
        os.link(marker, development_root / ".second-owner-name")
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_DEV_SECRETS_DIR": str(secrets),
        "VONK_TEST_CAPTURE_DIR": str(capture),
    }

    result = subprocess.run(
        (str(local_script), "config"),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "ownership marker" in result.stderr
    assert not (capture / "environment").exists()


@pytest.mark.parametrize("component", ("wrapper", "origin", "marker"))
def test_dev_compose_script_rejects_symlinked_origin_components(
    tmp_path: Path, component: str
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin, capture = _fake_docker(tmp_path)
    development_root = repository / ".dev"
    external = tmp_path / f"external-{component}"
    if component == "wrapper":
        external.mkdir(mode=0o700)
        development_root.symlink_to(external, target_is_directory=True)
    else:
        development_root.mkdir(mode=0o700)
        origin = development_root / "vonk-forge-origin.git"
        if component == "origin":
            subprocess.run(("git", "init", "--bare", "-q", str(external)), check=True)
            external.chmod(0o700)
            origin.symlink_to(external, target_is_directory=True)
        else:
            subprocess.run(("git", "init", "--bare", "-q", str(origin)), check=True)
            origin.chmod(0o700)
        marker = development_root / ".vonk-forge-origin.owner"
        if component == "marker":
            external.write_bytes(b"vonk-forge-dev-origin-v1\n")
            external.chmod(0o600)
            marker.symlink_to(external)
        else:
            marker.write_bytes(b"vonk-forge-dev-origin-v1\n")
            marker.chmod(0o600)
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_DEV_SECRETS_DIR": str(secrets),
        "VONK_TEST_CAPTURE_DIR": str(capture),
    }

    result = subprocess.run(
        (str(local_script), "config"),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not (capture / "environment").exists()
