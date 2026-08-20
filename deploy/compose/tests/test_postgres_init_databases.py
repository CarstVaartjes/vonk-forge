from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "deploy/compose/postgres/init-databases.sh"
POSTGRES_IMAGE = (
    "postgres:18.3@sha256:"
    "7e32e9833a6fb1c92c32552794cb6ed569d51b445a54907d35fc112ef39684db"
)


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
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >\"$CALLS\"\n"
        "cat >>\"$CALLS\"\n",
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


def test_fresh_postgres_owns_a_distinct_litellm_database(tmp_path: Path) -> None:
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
    password_file = tmp_path / "litellm-password"
    password_file.write_text("b" * 64 + "\n", encoding="ascii")
    password_file.chmod(0o600)
    try:
        container = subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "-e",
                "POSTGRES_DB=control",
                "-e",
                "POSTGRES_USER=control",
                "-e",
                "POSTGRES_PASSWORD=control-password",
                "-v",
                f"{SCRIPT}:/docker-entrypoint-initdb.d/10-vonk-forge-databases.sh:ro",
                "-v",
                f"{password_file}:/run/secrets/litellm-database-password:ro",
                POSTGRES_IMAGE,
            ],
            text=True,
            timeout=60,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise AssertionError(f"disposable PostgreSQL failed to start: {error}") from error
    try:
        for _ in range(120):
            probe = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "pg_isready",
                    "-U",
                    "control",
                    "-d",
                    "control",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if probe.returncode == 0:
                break
            time.sleep(0.25)
        else:
            logs = subprocess.run(
                ["docker", "logs", container],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            raise AssertionError(f"PostgreSQL did not become ready:\n{logs.stderr}")
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
    finally:
        subprocess.run(
            ["docker", "stop", container],
            check=False,
            capture_output=True,
            timeout=30,
        )
