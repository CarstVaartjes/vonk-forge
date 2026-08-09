from __future__ import annotations

import json
import os
import shutil
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
        "VONK_DEV_ORIGIN_DIR": str(tmp_path / "one-use-origin.git"),
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
        tmp_path / "one-use-origin.git"
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


def _fake_docker(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "printf '%s\\n%s\\n%s\\n' \"${VONK_DEV_EXPECTED_COMMIT:-}\" "
        "\"${VONK_DEV_ORIGIN_DIR:-absent}\" \"$VONK_DEV_SECRETS_DIR\" "
        "> \"$VONK_TEST_CAPTURE_DIR/environment\"\n"
        "git --git-dir=\"$VONK_DEV_ORIGIN_DIR\" rev-parse refs/heads/main "
        "> \"$VONK_TEST_CAPTURE_DIR/main\"\n"
        "stat -c %a \"$VONK_DEV_ORIGIN_DIR\" > \"$VONK_TEST_CAPTURE_DIR/mode\"\n"
        "printf '%s\\n' \"$@\" > \"$VONK_TEST_CAPTURE_DIR/arguments\"\n"
        "if [[ \" $* \" == *\" up \"* && \" $* \" != *\" --wait \"* ]]; then\n"
        "  exit 89\n"
        "fi\n"
        "exit \"${VONK_TEST_DOCKER_EXIT:-0}\"\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    return fake_bin


def _existing_secrets(tmp_path: Path) -> Path:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in ("postgres-password", "database-url", "git-signing-key"):
        (secrets / name).write_text("already-created\n", encoding="utf-8")
    return secrets


def test_dev_compose_script_uses_a_unique_one_use_origin_and_ignores_override(
    tmp_path: Path,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    arbitrary_origin = tmp_path / "arbitrary-origin.git"
    arbitrary_tmp = tmp_path / "attacker-controlled-tmp"
    arbitrary_tmp.mkdir()
    expected = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    origins: list[Path] = []
    for invocation in ("first", "second"):
        capture = tmp_path / f"capture-{invocation}"
        capture.mkdir()
        environment = os.environ | {
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "VONK_DEV_ORIGIN_DIR": str(arbitrary_origin),
            "VONK_DEV_SECRETS_DIR": str(secrets),
            "VONK_TEST_CAPTURE_DIR": str(capture),
            "TMPDIR": str(arbitrary_tmp),
        }

        subprocess.run(
            (str(local_script), "config"),
            cwd=repository,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

        exported_commit, origin, exported_secrets = (
            capture / "environment"
        ).read_text(encoding="utf-8").splitlines()
        origin_path = Path(origin)
        origins.append(origin_path)
        assert exported_commit == expected
        assert exported_secrets == str(secrets)
        assert origin_path.parent == Path("/tmp")
        assert origin_path.name.startswith("vonk-forge-dev-origin.")
        assert (capture / "main").read_text(encoding="utf-8").strip() == expected
        assert (capture / "mode").read_text(encoding="utf-8").strip() == "755"
        assert not origin_path.exists()
        arguments = (capture / "arguments").read_text(encoding="utf-8").splitlines()
        assert arguments[-1] == "config"
        assert str(repository / "deploy/compose/compose.dev.yaml") in arguments

    assert origins[0] != origins[1]
    assert not arbitrary_origin.exists()
    assert not (repository / ".dev/vonk-forge-origin.git").exists()
    assert not (repository / ".dev/.vonk-forge-origin.owner").exists()


def test_dev_compose_origin_is_not_redirected_when_dev_is_swapped_after_validation(
    tmp_path: Path,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp is not None
    fake_mktemp = fake_bin / "mktemp"
    fake_mktemp.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "mv -- \"$VONK_TEST_REPOSITORY_ROOT/.dev\" "
        "\"$VONK_TEST_REPOSITORY_ROOT/.dev.checked\"\n"
        "ln -s -- \"$VONK_TEST_SWAPPED_DEV\" "
        "\"$VONK_TEST_REPOSITORY_ROOT/.dev\"\n"
        f"exec {real_mktemp} \"$@\"\n",
        encoding="utf-8",
    )
    fake_mktemp.chmod(0o755)
    capture = tmp_path / "capture"
    capture.mkdir()
    swapped_dev = tmp_path / "attacker-development"
    swapped_dev.mkdir()
    sentinel = swapped_dev / "operator-owned"
    sentinel.write_text("preserve\n", encoding="utf-8")
    arbitrary_tmp = tmp_path / "attacker-controlled-tmp"
    arbitrary_tmp.mkdir()
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "TMPDIR": str(arbitrary_tmp),
        "VONK_DEV_SECRETS_DIR": str(secrets),
        "VONK_TEST_CAPTURE_DIR": str(capture),
        "VONK_TEST_REPOSITORY_ROOT": str(repository),
        "VONK_TEST_SWAPPED_DEV": str(swapped_dev),
    }

    result = subprocess.run(
        (str(local_script), "config"),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    origin = Path(
        (capture / "environment").read_text(encoding="utf-8").splitlines()[1]
    )
    assert result.returncode == 0
    assert origin.parent == Path("/tmp")
    assert origin.name.startswith("vonk-forge-dev-origin.")
    assert not origin.exists()
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"
    assert {path.name for path in swapped_dev.iterdir()} == {"operator-owned"}
    assert (repository / ".dev.checked").is_dir()


@pytest.mark.parametrize("service", ("dev-init", "migrate"))
def test_dev_compose_script_rejects_one_shot_only_targeted_up_before_origin_creation(
    tmp_path: Path, service: str
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp is not None
    fake_mktemp = fake_bin / "mktemp"
    fake_mktemp.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "touch \"$VONK_TEST_CAPTURE_DIR/mktemp-called\"\n"
        f"exec {real_mktemp} \"$@\"\n",
        encoding="utf-8",
    )
    fake_mktemp.chmod(0o755)
    capture = tmp_path / "capture"
    capture.mkdir()
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_DEV_SECRETS_DIR": str(secrets),
        "VONK_TEST_CAPTURE_DIR": str(capture),
    }

    result = subprocess.run(
        (str(local_script), "up", "-d", service),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert service in result.stderr
    assert "one-shot" in result.stderr
    assert not (capture / "environment").exists()
    assert not (capture / "mktemp-called").exists()


@pytest.mark.parametrize(
    ("arguments", "expected_tail"),
    (
        ((), ("up", "-d", "--build", "--wait")),
        (("up", "-d", "control-api"), ("up", "-d", "control-api", "--wait")),
    ),
)
def test_dev_compose_script_waits_for_every_supported_up_path(
    tmp_path: Path, arguments: tuple[str, ...], expected_tail: tuple[str, ...]
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_DEV_SECRETS_DIR": str(secrets),
        "VONK_TEST_CAPTURE_DIR": str(capture),
    }

    result = subprocess.run(
        (str(local_script), *arguments),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    origin = Path(
        (capture / "environment").read_text(encoding="utf-8").splitlines()[1]
    )
    compose_arguments = (capture / "arguments").read_text(
        encoding="utf-8"
    ).splitlines()
    assert result.returncode == 0
    assert tuple(compose_arguments[-len(expected_tail) :]) == expected_tail
    assert not origin.exists()


@pytest.mark.parametrize(
    "arguments",
    (
        ("create",),
        ("start",),
        ("run", "-d", "control-api"),
        ("restart",),
        ("up", "--no-start"),
        ("up", "--no-start=true"),
    ),
)
def test_dev_compose_script_rejects_lifecycles_without_a_safe_origin_lifetime(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_DEV_SECRETS_DIR": str(secrets),
        "VONK_TEST_CAPTURE_DIR": str(capture),
    }

    result = subprocess.run(
        (str(local_script), *arguments),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unsupported" in result.stderr
    assert not (capture / "environment").exists()


def test_dev_compose_script_cleans_one_use_origin_after_docker_failure(
    tmp_path: Path,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_DEV_SECRETS_DIR": str(secrets),
        "VONK_TEST_CAPTURE_DIR": str(capture),
        "VONK_TEST_DOCKER_EXIT": "37",
    }

    result = subprocess.run(
        (str(local_script), "config"),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    origin = Path(
        (capture / "environment").read_text(encoding="utf-8").splitlines()[1]
    )
    assert result.returncode == 37
    assert not origin.exists()


def test_dev_compose_script_rejects_a_symlinked_development_directory(
    tmp_path: Path,
) -> None:
    repository, local_script = _script_repository(tmp_path)
    secrets = _existing_secrets(tmp_path)
    fake_bin = _fake_docker(tmp_path)
    capture = tmp_path / "capture"
    capture.mkdir()
    development_root = repository / ".dev"
    external = tmp_path / "external-development"
    external.mkdir(mode=0o700)
    sentinel = external / "operator-owned"
    sentinel.write_text("preserve\n", encoding="utf-8")
    development_root.symlink_to(external, target_is_directory=True)
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
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"
    assert {path.name for path in external.iterdir()} == {"operator-owned"}
