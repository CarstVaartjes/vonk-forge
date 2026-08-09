from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-image-acceptance"
TEMPLATE = ROOT / "deploy/compose/compose.dev.images.yaml"
API_IMAGE = "vonk-forge-api:dev-local"
WORKER_IMAGE = "vonk-forge-worker:dev-local"
LOCAL_WRAPPER = ROOT / "scripts/dev-compose"


def _fixture_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    (repository / "deploy/compose").mkdir(parents=True)
    shutil.copy2(SCRIPT, repository / "scripts/dev-image-acceptance")
    shutil.copy2(ROOT / "scripts/verify-dev-image-secrets", repository / "scripts/verify-dev-image-secrets")
    shutil.copy2(TEMPLATE, repository / "deploy/compose/compose.dev.images.yaml")
    subprocess.run(("git", "init", "-q", "-b", "main", str(repository)), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.email", "test@example.invalid"),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.name", "Test"), check=True
    )
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", "."), check=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-qm", "main fixture"), check=True)
    return repository


def _commit(repository: Path, revision: str = "main") -> str:
    return subprocess.run(
        ("git", "-C", str(repository), "rev-parse", revision),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%q ' \"$@\" >> \"$VONK_TEST_DOCKER_LOG\"\n"
        "printf '\\n' >> \"$VONK_TEST_DOCKER_LOG\"\n"
        "if [[ \"$1\" == image ]]; then exit 47; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return fake_bin, log


def _config_capturing_docker(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%q ' \"$@\" >> \"$VONK_TEST_DOCKER_LOG\"\n"
        "printf '\\n' >> \"$VONK_TEST_DOCKER_LOG\"\n"
        "if [[ \"$1\" == image && \" $* \" == *' --format '* ]]; then\n"
        "  printf '%s\\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1\" == image ]]; then\n"
        "  printf '%s\\n' '[{\"Config\":{\"User\":\"10001:10001\",\"Env\":[],\"Labels\":{},\"Entrypoint\":[],\"Cmd\":[]}}]'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1\" == history ]]; then exit 0; fi\n"
        "if [[ \"$1\" == create ]]; then printf '%s\\n' scanner-container; exit 0; fi\n"
        "if [[ \"$1\" == export ]]; then tar -cf - --files-from /dev/null; exit 0; fi\n"
        "if [[ \"$1\" == rm || \"$1\" == ps ]]; then exit 0; fi\n"
        "if [[ \"$1\" == compose && \" $* \" == *' config '* ]]; then\n"
        "  number=0\n"
        "  while [[ \"$#\" -gt 0 ]]; do\n"
        "    if [[ \"$1\" == --file || \"$1\" == -f ]]; then\n"
        "      number=$((number + 1))\n"
        "      cp \"$2\" \"$VONK_TEST_DOCKER_LOG.compose-$number\"\n"
        "      shift 2\n"
        "    else\n"
        "      shift\n"
        "    fi\n"
        "  done\n"
        "  exit 48\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return fake_bin, log


def _failing_up_docker(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%q ' \"$@\" >> \"$VONK_TEST_DOCKER_LOG\"\n"
        "printf '\\n' >> \"$VONK_TEST_DOCKER_LOG\"\n"
        "if [[ \"$1\" == image && \" $* \" == *' --format '* ]]; then\n"
        "  printf '%s\\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1\" == image ]]; then\n"
        "  printf '%s\\n' '[{\"Config\":{\"User\":\"10001:10001\",\"Env\":[],\"Labels\":{},\"Entrypoint\":[],\"Cmd\":[]}}]'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1\" == history ]]; then exit 0; fi\n"
        "if [[ \"$1\" == create ]]; then printf '%s\\n' scanner-container; exit 0; fi\n"
        "if [[ \"$1\" == export ]]; then tar -cf - --files-from /dev/null; exit 0; fi\n"
        "if [[ \"$1\" == rm || \"$1\" == ps ]]; then exit 0; fi\n"
        "if [[ \"$1\" == compose && \" $* \" == *' up '* ]]; then exit 49; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return fake_bin, log


def _run(
    repository: Path, fake_bin: Path, log: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_TEST_DOCKER_LOG": str(log),
    }
    return subprocess.run(
        (str(repository / "scripts/dev-image-acceptance"), *arguments),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def test_rejects_missing_or_production_image_arguments_before_calling_docker(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    fake_bin, log = _fake_docker(tmp_path)
    commit = _commit(repository)

    missing = _run(repository, fake_bin, log)
    production = _run(
        repository,
        fake_bin,
        log,
        "--api-image",
        "ghcr.io/carstvaartjes/vonk-forge-api:v1.0.0@sha256:" + "a" * 64,
        "--worker-image",
        WORKER_IMAGE,
        "--commit",
        commit,
    )

    assert missing.returncode != 0
    assert production.returncode != 0
    assert not log.exists()


def test_rejects_a_commit_that_is_not_the_local_main_tip_before_calling_docker(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    fake_bin, log = _fake_docker(tmp_path)
    subprocess.run(("git", "-C", str(repository), "checkout", "-qb", "feature"), check=True)
    (repository / "feature.txt").write_text("not main\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", "feature.txt"), check=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-qm", "feature fixture"), check=True)

    result = _run(
        repository,
        fake_bin,
        log,
        "--api-image",
        API_IMAGE,
        "--worker-image",
        WORKER_IMAGE,
        "--commit",
        _commit(repository, "HEAD"),
    )

    assert result.returncode != 0
    assert "main" in result.stderr
    assert not log.exists()


def test_installs_cleanup_before_image_inspection_and_uses_unique_project_teardown(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    fake_bin, log = _fake_docker(tmp_path)
    commit = _commit(repository)
    arguments = (
        "--api-image",
        API_IMAGE,
        "--worker-image",
        WORKER_IMAGE,
        "--commit",
        commit,
    )

    first = _run(repository, fake_bin, log, *arguments)
    second = _run(repository, fake_bin, log, *arguments)

    assert first.returncode == 1
    assert second.returncode == 1
    commands = log.read_text(encoding="utf-8").splitlines()
    image_commands = [command for command in commands if command.startswith("image inspect")]
    down_commands = [command for command in commands if " down " in f" {command} "]
    assert len(image_commands) == 2
    assert len(down_commands) == 2
    assert all("--volumes" in command for command in down_commands)
    assert all("--remove-orphans" in command for command in down_commands)
    projects = {
        command.split("-p ", 1)[1].split(" ", 1)[0]
        for command in down_commands
    }
    assert len(projects) == 2
    assert all(project.startswith("vonk-forge-accept-") for project in projects)
    assert all(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project) for project in projects)


def test_rejects_an_untrusted_temporary_workspace_before_calling_docker(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    fake_bin, log = _fake_docker(tmp_path)
    unsafe = tmp_path / "not-an-acceptance-workspace"
    unsafe.mkdir()
    mktemp = fake_bin / "mktemp"
    mktemp.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$VONK_TEST_UNSAFE_WORKSPACE\"\n",
        encoding="utf-8",
    )
    mktemp.chmod(0o755)

    result = _run(
        repository,
        fake_bin,
        log,
        "--api-image",
        API_IMAGE,
        "--worker-image",
        WORKER_IMAGE,
        "--commit",
        _commit(repository),
    )

    assert result.returncode != 0
    assert "temporary workspace" in result.stderr
    assert not log.exists()


def test_renders_local_origin_override_as_a_separate_temporary_compose_overlay(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    fake_bin, log = _config_capturing_docker(tmp_path)

    result = _run(
        repository,
        fake_bin,
        log,
        "--api-image",
        API_IMAGE,
        "--worker-image",
        WORKER_IMAGE,
        "--commit",
        _commit(repository),
    )

    assert result.returncode == 48
    base = Path(f"{log}.compose-1").read_text(encoding="utf-8")
    overlay = Path(f"{log}.compose-2").read_text(encoding="utf-8")
    assert base.count("\nservices:") == 1
    assert "VONK_DEV_LOCAL_ACCEPTANCE" not in base
    assert "file:///source-origin" not in base
    assert "VONK_DEV_LOCAL_ACCEPTANCE" in overlay
    assert "file:///source-origin" in overlay
    assert "VONK_DEV_API_IMAGE" in overlay
    assert "VONK_DEV_WORKER_IMAGE" in overlay


def test_failed_compose_start_prints_bounded_diagnostics_before_teardown(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    fake_bin, log = _failing_up_docker(tmp_path)

    result = _run(
        repository,
        fake_bin,
        log,
        "--api-image",
        API_IMAGE,
        "--worker-image",
        WORKER_IMAGE,
        "--commit",
        _commit(repository),
    )

    commands = log.read_text(encoding="utf-8")
    assert result.returncode == 49
    assert " ps -a " in f" {commands} "
    assert " logs --tail 100 --no-color " in f" {commands} "
    assert " down --volumes --remove-orphans " in f" {commands} "


def test_local_wrapper_runs_the_image_only_template_without_a_source_origin() -> None:
    text = LOCAL_WRAPPER.read_text(encoding="utf-8")

    assert "compose.dev.images.yaml" in text
    assert "vonk-forge-api:dev-local" in text
    assert "vonk-forge-worker:dev-local" in text
    assert "--build" not in text
    assert "VONK_DEV_ORIGIN_DIR" not in text
    assert "VONK_DEV_LOCAL_ACCEPTANCE" not in text
    assert "file:///source-origin" not in text


def test_acceptance_runs_the_task_three_scanner_and_never_requests_image_mutation() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'readonly scanner="$repository_root/scripts/verify-dev-image-secrets"' in text
    assert '"$scanner" "$api_image" "$worker_image"' in text
    assert "compose up --wait --pull never" in text
    assert "docker build" not in text
    assert "docker pull" not in text
    assert "docker push" not in text
    assert "docker tag" not in text
    assert "docker image rm" not in text


def test_acceptance_diagnostics_are_bounded_and_avoid_raw_secret_output() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "logs --tail 100 --no-color" in text
    assert "head -c 32768" in text
    assert "[redacted]" in text


def test_acceptance_checks_container_mounts_without_stealing_inspect_stdin() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "docker inspect --format '{{json .Mounts}}'" in text
    assert "| python3 -c" in text
    assert "docker inspect \"$api_container\" | python3 -" not in text
