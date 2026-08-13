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
SOURCE_REPOSITORY = "https://github.com/CarstVaartjes/vonk-forge"

_FIXTURE_IMAGE_INSPECTION = (
    'if [[ "$1" == image && " $* " == *\'.RepoDigests\'* ]]; then\n'
    '  tag="${@: -1}"\n'
    "  repository=${tag%:*}\n"
    '  case "$tag" in\n'
    "    *vonk-forge-api:cohort-a) digest='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;\n"
    "    *vonk-forge-worker:cohort-a) digest='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' ;;\n"
    "    *vonk-forge-api:cohort-b) digest='cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' ;;\n"
    "    *) digest='dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd' ;;\n"
    "  esac\n"
    '  printf \'%s@sha256:%s\\n\' "$repository" "$digest"\n'
    "  exit 0\n"
    "fi\n"
    'if [[ "$1" == image && " $* " == *"{{.Id}}"* ]]; then\n'
    '  reference="${@: -1}"\n'
    '  case "$reference" in\n'
    "    *@sha256:aaaa*) digit=1 ;;\n"
    "    *@sha256:bbbb*) digit=2 ;;\n"
    "    *@sha256:cccc*) digit=3 ;;\n"
    "    *) digit=4 ;;\n"
    "  esac\n"
    "  printf 'sha256:%064d\\n' \"$digit\"\n"
    "  exit 0\n"
    "fi\n"
)


def test_cohort_harness_uses_an_explicit_browser_authority_without_tailscale() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert 'VONK_CONTROL_HOSTNAME_FILE: ""' in source
    assert "VONK_CONTROL_HOSTNAME: vonk-forge.acceptance.ts.net" in source
    assert "http://127.0.0.1:2019/healthz" in source
    assert "Host: vonk-forge.acceptance.ts.net" in source


def _install_successful_curl(fake_bin: Path, log: Path) -> None:
    curl = fake_bin / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf 'curl ' >> \"$VONK_TEST_DOCKER_LOG\"\n"
        'printf \'%q \' "$@" >> "$VONK_TEST_DOCKER_LOG"\n'
        "printf '\\n' >> \"$VONK_TEST_DOCKER_LOG\"\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)


def _fixture_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    (repository / "scripts").mkdir(parents=True)
    (repository / "deploy/compose").mkdir(parents=True)
    shutil.copy2(SCRIPT, repository / "scripts/dev-image-acceptance")
    shutil.copy2(
        ROOT / "scripts/verify-dev-image-secrets",
        repository / "scripts/verify-dev-image-secrets",
    )
    shutil.copy2(
        ROOT / "scripts/dev-runtime-secrets.py",
        repository / "scripts/dev-runtime-secrets.py",
    )
    (repository / "control/src/vonk_control").mkdir(parents=True)
    for module in ("__init__.py", "passwords.py"):
        shutil.copy2(
            ROOT / "control/src/vonk_control" / module,
            repository / "control/src/vonk_control" / module,
        )
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
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-qm", "main fixture"), check=True
    )
    (repository / "README.md").write_text("fixture update\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", "README.md"), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-qm", "main update fixture"),
        check=True,
    )
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
        'printf \'%q \' "$@" >> "$VONK_TEST_DOCKER_LOG"\n'
        "printf '\\n' >> \"$VONK_TEST_DOCKER_LOG\"\n"
        'if [[ "$1" == image ]]; then exit 47; fi\n'
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
        'printf \'%q \' "$@" >> "$VONK_TEST_DOCKER_LOG"\n'
        "printf '\\n' >> \"$VONK_TEST_DOCKER_LOG\"\n"
        + _FIXTURE_IMAGE_INSPECTION
        + 'if [[ "$1" == image && " $* " == *\' --format \'* ]]; then\n'
        "  printf '%s\\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == image ]]; then\n'
        '  printf \'[{"Config":{"User":"10001:10001","Env":[],"Labels":{"org.opencontainers.image.revision":"%s","org.opencontainers.image.source":"%s"},"Entrypoint":[],"Cmd":[]}}]\\n\' "$VONK_TEST_COMMIT" "$VONK_TEST_SOURCE_REPOSITORY"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == history ]]; then exit 0; fi\n'
        "if [[ \"$1\" == create ]]; then printf '%s\\n' scanner-container; exit 0; fi\n"
        'if [[ "$1" == export ]]; then tar -cf - --files-from /dev/null; exit 0; fi\n'
        'if [[ "$1" == rm || "$1" == ps ]]; then exit 0; fi\n'
        'if [[ "$1" == run && " $* " == *\' --network=none \'* ]]; then\n'
        '  printf \'{"image_role":"%s","source_commit":"%s","source_repository":"%s"}\\n\' "${@: -3:1}" "${@: -2:1}" "${@: -1}"\n'
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1\" == run ]]; then printf '%s\\n' '{}'; exit 0; fi\n"
        'if [[ "$1" == compose && " $* " == *\' config \'* ]]; then\n'
        "  number=0\n"
        '  while [[ "$#" -gt 0 ]]; do\n'
        '    if [[ "$1" == --file || "$1" == -f ]]; then\n'
        "      number=$((number + 1))\n"
        '      cp "$2" "$VONK_TEST_DOCKER_LOG.compose-$number"\n'
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
    _install_successful_curl(fake_bin, log)
    return fake_bin, log


def _failing_up_docker(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'printf \'%q \' "$@" >> "$VONK_TEST_DOCKER_LOG"\n'
        "printf '\\n' >> \"$VONK_TEST_DOCKER_LOG\"\n"
        + _FIXTURE_IMAGE_INSPECTION
        + 'if [[ "$1" == image && " $* " == *\' --format \'* ]]; then\n'
        "  printf '%s\\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == image ]]; then\n'
        '  printf \'[{"Config":{"User":"10001:10001","Env":[],"Labels":{"org.opencontainers.image.revision":"%s","org.opencontainers.image.source":"%s"},"Entrypoint":[],"Cmd":[]}}]\\n\' "$VONK_TEST_COMMIT" "$VONK_TEST_SOURCE_REPOSITORY"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == history ]]; then exit 0; fi\n'
        "if [[ \"$1\" == create ]]; then printf '%s\\n' scanner-container; exit 0; fi\n"
        'if [[ "$1" == export ]]; then tar -cf - --files-from /dev/null; exit 0; fi\n'
        'if [[ "$1" == rm || "$1" == ps ]]; then exit 0; fi\n'
        'if [[ "$1" == run && " $* " == *\' --network=none \'* ]]; then\n'
        '  printf \'{"image_role":"%s","source_commit":"%s","source_repository":"%s"}\\n\' "${@: -3:1}" "${@: -2:1}" "${@: -1}"\n'
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1\" == run ]]; then printf '%s\\n' '{}'; exit 0; fi\n"
        'if [[ "$1" == compose && " $* " == *\' logs \'* ]]; then\n'
        "  printf '%s\\n' \\\n"
        "    'api | -----BEGIN OPENSSH PRIVATE KEY-----' \\\n"
        "    'api | QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFB' \\\n"
        "    'api | -----END OPENSSH PRIVATE KEY-----' \\\n"
        "    'worker | dGhpcy1pcy1hLXN5bnRoZXRpYy13b3JrZXItdG9rZW4tMTIzNDU2Nzg5MA' \\\n"
        "    'proxy | Authorization: Bearer abcdefghijklmnop' \\\n"
        "    'migrate | postgresql+psycopg://control:visible@postgres:5432/control'\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == compose && " $* " == *\' up \'* ]]; then exit 49; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    _install_successful_curl(fake_bin, log)
    return fake_bin, log


def _successful_lifecycle_tools(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'printf \'%q \' "$@" >> "$VONK_TEST_DOCKER_LOG"\n'
        "printf '\\n' >> \"$VONK_TEST_DOCKER_LOG\"\n"
        'state_file="$VONK_TEST_DOCKER_LOG.state"\n'
        'reset_file="$VONK_TEST_DOCKER_LOG.reset"\n'
        + _FIXTURE_IMAGE_INSPECTION
        + 'if [[ "$1" == image && " $* " == *\' --format \'* ]]; then\n'
        "  printf '%s\\n' 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == image ]]; then\n'
        '  printf \'[{"Config":{"User":"10001:10001","Env":[],"Labels":{"org.opencontainers.image.revision":"%s","org.opencontainers.image.source":"%s"},"Entrypoint":[],"Cmd":[]}}]\\n\' "$VONK_TEST_COMMIT" "$VONK_TEST_SOURCE_REPOSITORY"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == history ]]; then exit 0; fi\n'
        "if [[ \"$1\" == create ]]; then printf '%s\\n' scanner-container; exit 0; fi\n"
        'if [[ "$1" == export ]]; then tar -cf - --files-from /dev/null; exit 0; fi\n'
        'if [[ "$1" == rm || "$1" == build || "$1" == tag || "$1" == push ]]; then exit 0; fi\n'
        'if [[ "$1" == volume && " $* " == *\' rm \'* ]]; then touch "$reset_file"; exit 0; fi\n'
        'if [[ "$1" == volume && " $* " == *\' inspect \'* ]]; then\n'
        "  if [[ \" $* \" == *' --format '* ]]; then\n"
        '    volume="${@: -1}"\n'
        "    project=${volume%_*}\n"
        "    logical=${volume##*_}\n"
        '    printf \'%s %s\\n\' "$logical" "$project"\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == run && " $* " == *\' --network=none \'* ]]; then\n'
        "  if [[ \" $* \" == *database_revision* ]]; then printf '%s\\n' 0021_browser_authentication; exit 0; fi\n"
        '  printf \'{"image_role":"%s","source_commit":"%s","source_repository":"%s"}\\n\' "${@: -3:1}" "${@: -2:1}" "${@: -1}"\n'
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1\" == run ]]; then printf '%s\\n' '{}'; exit 0; fi\n"
        'if [[ "$1" == ps ]]; then exit 0; fi\n'
        'if [[ "$1" == inspect && " $* " == *\'{{json .Mounts}}\'* ]]; then\n'
        '  container="${@: -1}"\n'
        "  project=$(sed -n 's/.*-p \\([^ ]*\\).*/\\1/p' \"$VONK_TEST_DOCKER_LOG\" | tail -n 1)\n"
        '  case "$container" in\n'
        '    postgres-id) printf \'[{"Destination":"/var/lib/postgresql","Type":"volume","Name":"%s_dev-postgres-data","Source":"%s_dev-postgres-data"}]\\n\' "$project" "$project" ;;\n'
        '    control-api-id) printf \'[{"Destination":"/repository","Type":"volume","Name":"%s_dev-repository","Source":"%s_dev-repository"},{"Destination":"/state","Type":"volume","Name":"%s_dev-control-state","Source":"%s_dev-control-state"}]\\n\' "$project" "$project" "$project" "$project" ;;\n'
        '    *) printf \'[{"Destination":"/repository","Type":"volume","Name":"%s_dev-repository","Source":"%s_dev-repository"}]\\n\' "$project" "$project" ;;\n'
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == inspect && " $* " == *\'{{.Image}}\'* ]]; then\n'
        "  cohort=$(sed -n '1p' \"$state_file\" 2>/dev/null || true)\n"
        '  container="${@: -1}"\n'
        '  if [[ "$cohort" == b ]]; then api_digit=3; worker_digit=4; else api_digit=1; worker_digit=2; fi\n'
        '  if [[ "$container" == control-api-id ]]; then printf \'sha256:%064d\\n\' "$api_digit"; else printf \'sha256:%064d\\n\' "$worker_digit"; fi\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == inspect ]]; then\n'
        "  cohort=$(sed -n '1p' \"$state_file\" 2>/dev/null || true)\n"
        '  case "${*: -1}" in\n'
        "    dev-repository-init-id) if [[ \"$cohort\" == rollback-failed ]]; then printf '%s\\n' 'exited 1'; else printf '%s\\n' 'exited 0'; fi ;;\n"
        "    dev-init-id) if [[ \"$cohort\" == rollback-failed ]]; then printf '%s\\n' 'created 0'; else printf '%s\\n' 'exited 0'; fi ;;\n"
        "    migrate-id) if [[ \"$cohort\" == rollback-failed ]]; then printf '%s\\n' 'created 0'; else printf '%s\\n' 'exited 0'; fi ;;\n"
        "    dev-auth-init-id) if [[ \"$cohort\" == rollback-failed ]]; then printf '%s\\n' 'created 0'; else printf '%s\\n' 'exited 0'; fi ;;\n"
        "    dev-*-id) printf '%s\\n' 'exited 0' ;;\n"
        "    *) printf '%s\\n' 'running 0' ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == compose && " $* " == *\' ps -aq \'* ]]; then\n'
        "  printf '%s-id\\n' \"${*: -1}\"\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == compose && " $* " == *\' exec \'* ]]; then\n'
        '  case " $* " in\n'
        "    *' git -C /repository rev-parse '*)\n"
        '      if [[ " $* " == *docker-compose.pinned.yaml* ]]; then\n'
        "        printf '%s\\n' \"$VONK_TEST_PREVIOUS_COMMIT\"\n"
        "      elif grep -q -- '--force-recreate' \"$VONK_TEST_DOCKER_LOG\"; then\n"
        "        printf '%s\\n' \"$VONK_TEST_COMMIT\"\n"
        "      else\n"
        "        printf '%s\\n' \"$VONK_TEST_PREVIOUS_COMMIT\"\n"
        "      fi ;;\n"
        "    *'find /control-identity'*) printf '%s\\n' identity-fingerprint ;;\n"
        "    *' psql '*) printf '%s\\n' 16384 ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == compose && " $* " == *\' logs \'*dev-repository-init* ]]; then\n'
        "  printf '%s\\n' 'development repository accepted baseline is divergent'\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == compose && " $* " == *\' up \'*dev-cohort-verify* ]]; then exit 50; fi\n'
        'if [[ "$1" == compose && " $* " == *\' up \'* ]]; then\n'
        '  if [[ " $* " == *docker-compose.pinned.yaml* && ! -e "$reset_file" ]]; then\n'
        "    printf '%s\\n' rollback-failed > \"$state_file\"\n"
        "    exit 51\n"
        "  fi\n"
        '  if [[ " $* " == *docker-compose.pinned.yaml* ]]; then printf \'%s\\n\' a > "$state_file"\n'
        "  elif [[ \" $* \" == *' --force-recreate '* ]]; then printf '%s\\n' b > \"$state_file\"\n"
        "  else printf '%s\\n' a > \"$state_file\"; fi\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == compose ]]; then exit 0; fi\n'
        "exit 97\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    _install_successful_curl(fake_bin, log)
    return fake_bin, log


def _run(
    repository: Path,
    fake_bin: Path,
    log: Path,
    *arguments: str,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    commit = next(
        (
            arguments[index + 1]
            for index, value in enumerate(arguments)
            if value == "--commit"
        ),
        "",
    )
    environment = os.environ | {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "VONK_TEST_DOCKER_LOG": str(log),
        "VONK_TEST_COMMIT": commit,
        "VONK_TEST_SOURCE_REPOSITORY": SOURCE_REPOSITORY,
    }
    if extra_environment:
        environment.update(extra_environment)
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
    subprocess.run(
        ("git", "-C", str(repository), "checkout", "-qb", "feature"), check=True
    )
    (repository / "feature.txt").write_text("not main\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", "feature.txt"), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-qm", "feature fixture"), check=True
    )

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


def test_rejects_feature_branch_code_even_when_supplied_commit_is_main(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    fake_bin, log = _fake_docker(tmp_path)
    main_commit = _commit(repository)
    subprocess.run(
        ("git", "-C", str(repository), "checkout", "-qb", "feature"), check=True
    )
    (repository / "feature.txt").write_text("feature source\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", "feature.txt"), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-qm", "feature source"), check=True
    )

    result = _run(
        repository,
        fake_bin,
        log,
        "--api-image",
        API_IMAGE,
        "--worker-image",
        WORKER_IMAGE,
        "--commit",
        main_commit,
    )

    assert result.returncode != 0
    assert "HEAD" in result.stderr
    assert not log.exists()


def test_rejects_a_dirty_main_worktree_before_calling_docker(tmp_path: Path) -> None:
    repository = _fixture_repository(tmp_path)
    fake_bin, log = _fake_docker(tmp_path)
    commit = _commit(repository)
    (repository / "untracked.txt").write_text("dirty source\n", encoding="utf-8")

    result = _run(
        repository,
        fake_bin,
        log,
        "--api-image",
        API_IMAGE,
        "--worker-image",
        WORKER_IMAGE,
        "--commit",
        commit,
    )

    assert result.returncode != 0
    assert "clean" in result.stderr
    assert not log.exists()


def test_scanner_receives_authoritative_commit_and_repository_before_lifecycle_fixtures(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    fake_bin, log = _fake_docker(tmp_path)
    scanner_log = tmp_path / "scanner.log"
    scanner = repository / "scripts/verify-dev-image-secrets"
    scanner.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'printf \'%s\\n\' "$@" > "$VONK_TEST_SCANNER_LOG"\n'
        "exit 73\n",
        encoding="utf-8",
    )
    scanner.chmod(0o755)
    subprocess.run(("git", "-C", str(repository), "add", str(scanner)), check=True)
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-qm", "scanner probe"), check=True
    )
    commit = _commit(repository)

    result = _run(
        repository,
        fake_bin,
        log,
        "--api-image",
        API_IMAGE,
        "--worker-image",
        WORKER_IMAGE,
        "--commit",
        commit,
        extra_environment={"VONK_TEST_SCANNER_LOG": str(scanner_log)},
    )

    assert result.returncode == 73
    assert scanner_log.read_text(encoding="utf-8").splitlines() == [
        "--expected-commit",
        commit,
        "--expected-repository",
        SOURCE_REPOSITORY,
        API_IMAGE,
        WORKER_IMAGE,
    ]
    docker_commands = log.read_text(encoding="utf-8").splitlines()
    assert docker_commands
    assert any(" down " in f" {command} " for command in docker_commands)
    assert not any("build-identity" in command for command in docker_commands)
    assert not any(
        action in f" {command} "
        for command in docker_commands
        for action in (" up ", " pull ", " exec ")
    )


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
    image_commands = [
        command for command in commands if command.startswith("image inspect")
    ]
    down_commands = [command for command in commands if " down " in f" {command} "]
    assert len(image_commands) == 2
    assert len(down_commands) == 2
    assert all("--volumes" in command for command in down_commands)
    assert all("--remove-orphans" in command for command in down_commands)
    projects = {
        command.split("-p ", 1)[1].split(" ", 1)[0] for command in down_commands
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
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$VONK_TEST_UNSAFE_WORKSPACE\"\n",
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
    assert "VONK_DEV_API_IMAGE" not in overlay
    assert "VONK_DEV_WORKER_IMAGE" not in overlay
    assert "VONK_CONTROL_PROCESS_IMAGE" not in overlay
    assert "x-pinned-expected-commit" not in base
    assert "__VONK_EXPECTED_COMMIT__" not in base
    assert "Compatibility input for the current pinned renderer" not in base


def test_public_acceptance_uses_selected_cohort_instead_of_rendered_identity_inputs(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    fake_bin, log = _config_capturing_docker(tmp_path)
    commit = _commit(repository)
    api_image = (
        f"ghcr.io/carstvaartjes/vonk-forge-api:dev-sha-{commit}@sha256:" + "a" * 64
    )
    worker_image = (
        f"ghcr.io/carstvaartjes/vonk-forge-worker:dev-sha-{commit}@sha256:" + "b" * 64
    )

    result = _run(
        repository,
        fake_bin,
        log,
        "--api-image",
        api_image,
        "--worker-image",
        worker_image,
        "--commit",
        commit,
    )

    assert result.returncode == 48
    overlay = Path(f"{log}.compose-2").read_text(encoding="utf-8")
    assert api_image not in overlay
    assert worker_image not in overlay
    assert "VONK_DEV_EXPECTED_COMMIT" not in overlay
    assert "VONK_DEV_SELECTED_COHORT_FILE" not in overlay


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
    assert "BEGIN OPENSSH PRIVATE KEY" not in result.stderr
    assert "QUFBQUFBQUFBQUFB" not in result.stderr
    assert "dGhpcy1pcy1hLXN5bnRoZXRpYy13b3JrZXI" not in result.stderr
    assert "abcdefghijklmnop" not in result.stderr
    assert "control:visible" not in result.stderr
    assert "[redacted]" in result.stderr


def test_mixed_cohort_gate_stops_before_initializer_or_migration(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    fake_bin, log = _successful_lifecycle_tools(tmp_path)
    commit = _commit(repository)
    previous_commit = _commit(repository, "main^")

    result = _run(
        repository,
        fake_bin,
        log,
        "--api-image",
        API_IMAGE,
        "--worker-image",
        WORKER_IMAGE,
        "--commit",
        commit,
        extra_environment={
            "VONK_TEST_COMMIT": commit,
            "VONK_TEST_PREVIOUS_COMMIT": previous_commit,
        },
    )

    commands = log.read_text(encoding="utf-8").splitlines()
    mixed_up = next(
        index
        for index, command in enumerate(commands)
        if " up " in f" {command} " and "dev-cohort-verify" in command
    )
    first_full_up = next(
        index
        for index, command in enumerate(commands)
        if " up " in f" {command} " and "dev-cohort-verify" not in command
    )

    assert result.returncode == 0, result.stderr
    assert mixed_up < first_full_up
    assert any(
        " ps --all --services dev-repository-init dev-init migrate " in f" {command} "
        for command in commands[mixed_up:first_full_up]
    )
    assert any(
        " down --volumes --remove-orphans " in f" {command} "
        for command in commands[mixed_up:first_full_up]
    )


def test_successful_lifecycle_exercises_mutable_redeploy_and_runtime_boundaries(
    tmp_path: Path,
) -> None:
    repository = _fixture_repository(tmp_path)
    fake_bin, log = _successful_lifecycle_tools(tmp_path)
    commit = _commit(repository)
    previous_commit = _commit(repository, "main^")

    result = _run(
        repository,
        fake_bin,
        log,
        "--api-image",
        API_IMAGE,
        "--worker-image",
        WORKER_IMAGE,
        "--commit",
        commit,
        extra_environment={
            "VONK_TEST_COMMIT": commit,
            "VONK_TEST_PREVIOUS_COMMIT": previous_commit,
        },
    )

    commands = log.read_text(encoding="utf-8").splitlines()
    normalized_commands = [line.replace("\\", "") for line in commands]
    assert result.returncode == 0, result.stderr
    assert "development image acceptance passed" in result.stdout
    assert sum(" up --wait --pull never " in f" {line} " for line in commands) == 5
    assert sum(" pull --policy always " in f" {line} " for line in commands) == 3
    assert sum(" --force-recreate " in f" {line} " for line in commands) == 3
    assert not any(
        " restart control-api control-worker " in f" {line} " for line in commands
    )
    assert sum(line.startswith("curl ") for line in commands) == 3
    assert sum("build-identity" in line for line in commands) == 4
    assert sum(line.startswith("build --pull=false ") for line in commands) == 4
    assert sum(line.startswith("tag ") and ":dev " in line for line in commands) == 6
    assert sum(line.startswith("push ") for line in commands) == 10
    assert (
        sum(line.startswith("image rm ") and ":dev " in line for line in commands) >= 6
    )
    assert sum("{{.Image}}" in line for line in normalized_commands) == 6
    repository_removals = [
        line for line in commands if line.startswith("volume rm -- ")
    ]
    assert len(repository_removals) == 1
    assert repository_removals[0].endswith("_dev-repository ")
    assert not any("ghcr.io" in line for line in commands if line.startswith("tag "))
    for service in (
        "dev-cohort-reset",
        "dev-api-cohort",
        "dev-worker-cohort",
        "dev-cohort-verify",
    ):
        assert (
            sum(
                "{{.State.Status}} {{.State.ExitCode}}" in line
                and f"{service}-id" in line
                for line in normalized_commands
            )
            == 3
        )
    assert any(
        "test -r /run/secrets/git-signing-key" in line for line in normalized_commands
    )
    assert any(
        "test -r /run/secrets/admin-grant-private-key" in line
        for line in normalized_commands
    )
    assert any(
        "test -r /run/secrets/worker-api-token" in line for line in normalized_commands
    )
    assert any(
        "test -r /run/secrets/token-signing-key" in line for line in normalized_commands
    )
    assert sum("projected secret" in line for line in normalized_commands) == 6
    assert sum("{{json .Mounts}}" in line for line in normalized_commands) == 9
    assert (
        sum(
            "{{json .Mounts}}" in line and "control-api-id" in line
            for line in normalized_commands
        )
        == 5
    )
    assert (
        sum(
            "{{json .Mounts}}" in line and "control-worker-id" in line
            for line in normalized_commands
        )
        == 3
    )
    assert (
        sum(
            "{{json .Mounts}}" in line and "postgres-id" in line
            for line in normalized_commands
        )
        == 1
    )
    worker_boundary_commands = [
        line
        for line in normalized_commands
        if "compose" in line and "exec -T control-worker" in line
    ]
    assert worker_boundary_commands
    assert all("git-signing-key" in line for line in worker_boundary_commands)
    assert all("admin-grant-private-key" in line for line in worker_boundary_commands)
    assert all("token-signing-key" in line for line in worker_boundary_commands)
    assert any(" down --volumes --remove-orphans " in f" {line} " for line in commands)


def test_local_wrapper_uses_a_one_use_source_origin_without_building() -> None:
    text = LOCAL_WRAPPER.read_text(encoding="utf-8")

    assert "compose.dev.images.yaml" in text
    assert "vonk-forge-api:dev-local" in text
    assert "vonk-forge-worker:dev-local" in text
    assert "--build" not in text
    assert "VONK_DEV_ORIGIN_DIR" not in text
    assert "VONK_DEV_LOCAL_ACCEPTANCE" in text
    assert "file:///source-origin" in text


def test_acceptance_scans_authoritative_inputs_before_mutating_randomized_fixture_images() -> (
    None
):
    text = SCRIPT.read_text(encoding="utf-8")

    assert (
        'readonly scanner="$repository_root/scripts/verify-dev-image-secrets"' in text
    )
    assert (
        'readonly expected_repository="https://github.com/CarstVaartjes/vonk-forge"'
        in text
    )
    assert '--expected-commit "$expected_commit"' in text
    assert '--expected-repository "$expected_repository"' in text
    assert '"$api_image" "$worker_image"' in text
    assert text.index('"$api_image" "$worker_image"') < text.index(
        'build_acceptance_image "$api_image"'
    )
    assert "FROM ${BASE_IMAGE}" in text
    assert (
        "COPY --chmod=0444 development-image-identity.json "
        "/usr/local/share/vonk-forge/development-image-identity.json"
    ) in text
    assert 'api_mutable_alias="$registry_scope/vonk-forge-api:dev"' in text
    assert 'worker_mutable_alias="$registry_scope/vonk-forge-worker:dev"' in text
    assert 'docker tag "$source_ref" "$alias"' in text
    assert 'docker push "$alias"' in text
    assert 'docker image rm "$alias"' in text
    assert "compose up --wait --pull never" in text
    assert "compose pull --policy always" in text
    assert "docker build" in text
    assert "docker pull" not in text
    assert ":/usr/local/share/vonk-forge/development-image-identity.json:ro" not in text
    assert 'docker tag "$api_image"' not in text
    assert 'docker tag "$worker_image"' not in text


def test_acceptance_exercises_pinned_rollback_and_deletes_only_the_verified_repository_volume() -> (
    None
):
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'api_image_a_ref=$(immutable_registry_ref "$api_image_a_tag")' in text
    assert 'worker_image_a_ref=$(immutable_registry_ref "$worker_image_a_tag")' in text
    assert (
        'render_acceptance_compose "$pinned_file" "$api_image_a_ref" '
        '"$worker_image_a_ref"'
    ) in text
    assert "development repository accepted baseline is divergent" in text
    assert 'assert_repository_only_reset_compatible "$api_image_a_ref"' in text
    assert 'read_identity(DEVELOPMENT_IMAGE_IDENTITY_PATH, expected_role="api")' in text
    assert "incompatible schemas require a matching full-state restore" in text
    assert "com.docker.compose.volume" in text
    assert "com.docker.compose.project" in text
    assert text.count('docker volume rm -- "$repository_volume"') == 1
    assert 'assert_volume_exists "$postgres_volume"' in text
    assert 'assert_volume_exists "$state_volume"' in text
    retention_block = text[
        text.index("for retained_volume in") : text.index(
            "# With only the verified repository volume reset"
        )
    ]
    assert "dev-tailscale-runtime" in retention_block
    assert "dev-tailscale-socket" not in retention_block
    assert "dev-tailscale-state" not in retention_block


def test_acceptance_diagnostics_are_bounded_and_avoid_raw_secret_output() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "logs --tail 100 --no-color" in text
    assert "head -c 32768" in text
    assert "[redacted]" in text


def test_acceptance_generates_synthetic_oauth_inputs_and_checks_only_projection_metadata() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'oauth_inputs="$acceptance_root/oauth-inputs"' in text
    assert '--tailscale-oauth-client-id-file "$oauth_inputs/client-id"' in text
    assert '--tailscale-oauth-client-secret-file "$oauth_inputs/client-secret"' in text
    assert "assert_projected_secret_metadata" in text
    assert '"dev-auth-secrets" "10001:10001" \\' in text
    assert '"database-url,admin-password-verifier"' in text
    assert '"dev-tailscale-secrets" "0:0" \\' in text
    assert '"tailscale-oauth-client-id,tailscale-oauth-client-secret"' in text
    metadata_function = text[
        text.index("assert_projected_secret_metadata()") : text.index(
            "repository_commit()"
        )
    ]
    assert "stat.S_IMODE(root.st_mode) != 0o550" in metadata_function
    assert "stat.S_IMODE(root.st_mode) != 0o700" not in metadata_function
    assert "read_text" not in metadata_function
    assert "read_bytes" not in metadata_function
    assert "open(" not in metadata_function


def test_acceptance_pins_the_temporary_registry_image_for_all_runner_architectures() -> (
    None
):
    text = SCRIPT.read_text(encoding="utf-8")

    pinned = (
        "registry:2.8.3@sha256:"
        "a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
    )
    assert pinned in text
    assert not re.search(r"registry:2(?:[\s\"'])", text)


def test_acceptance_checks_container_mounts_without_stealing_inspect_stdin() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "docker inspect --format '{{json .Mounts}}'" in text
    assert "| python3 -c" in text
    assert 'docker inspect "$api_container" | python3 -' not in text
