from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "deploy/compose/postgres/init-databases.sh"
ENTRYPOINT = ROOT / "deploy/compose/postgres/entrypoint.sh"
POSTGRES_IMAGE = json.loads(
    (ROOT / "deploy/compose/images.lock.json").read_text(encoding="utf-8")
)["images"]["postgres"]


def _docker_unavailable(message: str) -> None:
    if os.getenv("CI"):
        raise AssertionError(message)
    pytest.skip(message)


def test_database_initializer_passes_a_validated_secret_to_psql(tmp_path: Path) -> None:
    password_file = tmp_path / "litellm-password"
    password_file.write_text("a" * 64 + "\n", encoding="ascii")
    calls = tmp_path / "calls"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    psql = fake_bin / "psql"
    psql.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >"$CALLS"\ncat >>"$CALLS"\n',
        encoding="ascii",
    )
    psql.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "CALLS": str(calls),
            "LITELLM_DATABASE_PASSWORD_FILE": str(password_file),
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "POSTGRES_DB": "control",
            "POSTGRES_USER": "control",
        }
    )

    result = subprocess.run(
        ["sh", str(SCRIPT)],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    invocation = calls.read_text(encoding="ascii")
    assert "--username control --dbname control" in invocation
    assert "--set=litellm_password=" + "a" * 64 in invocation
    assert "CREATE ROLE litellm" in invocation
    assert "CREATE DATABASE litellm OWNER litellm" in invocation
    assert "ALTER ROLE litellm LOGIN PASSWORD" in invocation
    assert "ALTER DATABASE litellm OWNER TO litellm" in invocation


def test_database_initializer_rejects_an_invalid_password(tmp_path: Path) -> None:
    password_file = tmp_path / "litellm-password"
    password_file.write_text("not-a-generated-secret\n", encoding="ascii")

    result = subprocess.run(
        ["sh", str(SCRIPT)],
        check=False,
        capture_output=True,
        env={
            **os.environ,
            "LITELLM_DATABASE_PASSWORD_FILE": str(password_file),
            "POSTGRES_DB": "control",
            "POSTGRES_USER": "control",
        },
        text=True,
    )

    assert result.returncode != 0
    assert result.stderr == "LiteLLM database password is invalid\n"


@pytest.fixture(scope="module")
def postgres_image() -> str:
    """The pinned image, pulled beforehand by scripts/pull-test-images."""
    if shutil.which("docker") is None:
        _docker_unavailable("Docker is required for the fresh PostgreSQL test")
    docker_info = subprocess.run(
        ["docker", "info"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if docker_info.returncode != 0:
        _docker_unavailable("Docker is unavailable for the fresh PostgreSQL test")
    present = subprocess.run(
        ["docker", "image", "inspect", POSTGRES_IMAGE],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if present.returncode != 0:
        # Tests never download: CI and scripts/test-local pull pinned images
        # once, with retries, before the suite starts.
        _docker_unavailable(
            f"{POSTGRES_IMAGE} is not present; run scripts/pull-test-images postgres"
        )
    return POSTGRES_IMAGE


def test_fresh_postgres_owns_a_distinct_litellm_database(
    tmp_path: Path, postgres_image: str
) -> None:
    password_file = tmp_path / "litellm-password"
    password_file.write_text("b" * 64 + "\n", encoding="ascii")
    password_file.chmod(0o600)
    init_asset = tmp_path / "init-databases.sh"
    shutil.copyfile(SCRIPT, init_asset)
    # A restored NAS project folder may retain readable contents without a
    # host-executable file. The container must treat it as a source asset.
    init_asset.chmod(0o644)
    container = f"vonk-postgres-test-{uuid.uuid4().hex}"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-e",
                "POSTGRES_DB=control",
                "-e",
                "POSTGRES_USER=control",
                "-e",
                "POSTGRES_PASSWORD=control-password",
                "-v",
                f"{init_asset}:/run/vonk-source-assets/postgres/init-databases.sh:ro",
                "-v",
                f"{password_file}:/run/secrets/litellm-database-password:ro",
                "-v",
                f"{ENTRYPOINT}:/usr/local/bin/vonk-postgres-entrypoint:ro",
                "--entrypoint",
                "/usr/local/bin/vonk-postgres-entrypoint",
                postgres_image,
                "postgres",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise AssertionError(
            f"disposable PostgreSQL failed to start: {error}"
        ) from error
    try:
        for _ in range(120):
            logs = subprocess.run(
                ["docker", "logs", container],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "psql",
                    "-U",
                    "control",
                    "-d",
                    "control",
                    "-tAc",
                    "SELECT 1",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            # The official image briefly starts a temporary server while it runs
            # init scripts, then shuts that server down before starting PostgreSQL
            # for real.  A successful query alone can therefore race that planned
            # shutdown.  Require the image's completed-init marker as well.
            if (
                "PostgreSQL init process complete; ready for start up."
                in logs.stdout + logs.stderr
                and probe.returncode == 0
                and probe.stdout.strip() == "1"
            ):
                break
            time.sleep(0.25)
        else:
            raise AssertionError(
                f"PostgreSQL did not become ready:\n{logs.stdout}{logs.stderr}"
            )
        rows = subprocess.check_output(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "control",
                "-d",
                "control",
                "-tAc",
                (
                    "SELECT datname || ':' || pg_get_userbyid(datdba) "
                    "FROM pg_database WHERE datname IN ('control', 'litellm') "
                    "ORDER BY datname"
                ),
            ],
            text=True,
            timeout=10,
        ).splitlines()

        assert rows == ["control:control", "litellm:litellm"]
        staged_initializer = subprocess.check_output(
            [
                "docker",
                "exec",
                container,
                "stat",
                "-c",
                "%F:%u:%g:%a",
                "/docker-entrypoint-initdb.d/10-vonk-forge-databases.sh",
            ],
            text=True,
            timeout=10,
        ).strip()
        assert staged_initializer == "regular file:0:0:444"
        roles = subprocess.check_output(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "control",
                "-d",
                "control",
                "-tAc",
                (
                    "SELECT rolname FROM pg_roles "
                    "WHERE rolname IN ('control', 'litellm') ORDER BY rolname"
                ),
            ],
            text=True,
            timeout=10,
        ).splitlines()
        assert roles == ["control", "litellm"]

        subprocess.run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "control",
                "-d",
                "control",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "DROP DATABASE litellm;",
                "-c",
                "DROP ROLE litellm;",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        subprocess.run(
            ["docker", "restart", container],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        for _ in range(120):
            repaired = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "psql",
                    "-U",
                    "control",
                    "-d",
                    "control",
                    "-tAc",
                    (
                        "SELECT count(*) FROM pg_database AS databases "
                        "JOIN pg_roles AS owners ON owners.oid = databases.datdba "
                        "WHERE databases.datname = 'litellm' "
                        "AND owners.rolname = 'litellm'"
                    ),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if repaired.returncode == 0 and repaired.stdout.strip() == "1":
                break
            time.sleep(0.25)
        else:
            raise AssertionError("PostgreSQL restart did not restore LiteLLM objects")
        repaired_database = subprocess.check_output(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "control",
                "-d",
                "control",
                "-tAc",
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = 'litellm'",
            ],
            text=True,
            timeout=10,
        ).strip()
        assert repaired_database == "litellm"
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container],
            check=False,
            capture_output=True,
            timeout=30,
        )
