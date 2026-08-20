from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

POSTGRES_IMAGE = (
    "postgres:18.3@sha256:"
    "7e32e9833a6fb1c92c32552794cb6ed569d51b445a54907d35fc112ef39684db"
)
_POSTGRES_PASSWORD = "postgres"
_POSTGRES_PORT_TEMPLATE = (
    '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}'
)


def postgres_database_name() -> str:
    return f"vonk_test_{uuid.uuid4().hex}"


def _docker_unavailable(message: str) -> None:
    if os.getenv("CI"):
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def _run(
    command: list[str],
    *,
    timeout: float,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="session")
def postgres_server_engine() -> Iterator[Engine]:
    if shutil.which("docker") is None:
        _docker_unavailable("Docker is required for PostgreSQL integration tests")

    try:
        docker_info = _run(["docker", "info"], timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        _docker_unavailable(f"Docker is unavailable: {error}")
    if docker_info.returncode != 0:
        detail = docker_info.stderr.strip() or docker_info.stdout.strip()
        _docker_unavailable(f"Docker is unavailable: {detail}")

    container_name = f"vonk-control-tests-{uuid.uuid4().hex[:12]}"
    try:
        started = _run(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "--name",
                container_name,
                "-e",
                f"POSTGRES_PASSWORD={_POSTGRES_PASSWORD}",
                "-p",
                "127.0.0.1::5432",
                POSTGRES_IMAGE,
            ],
            timeout=180,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        pytest.fail(f"disposable PostgreSQL failed to start: {error}", pytrace=False)

    container = started.stdout.strip()
    admin_engine: Engine | None = None
    try:
        inspected = _run(
            ["docker", "inspect", "-f", _POSTGRES_PORT_TEMPLATE, container],
            timeout=15,
        )
        port = inspected.stdout.strip()
        admin_engine = create_engine(
            "postgresql+psycopg://"
            f"postgres:{_POSTGRES_PASSWORD}@127.0.0.1:{port}/postgres",
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=True,
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                with admin_engine.connect():
                    break
            except (OSError, SQLAlchemyError) as error:
                if time.monotonic() >= deadline:
                    pytest.fail(
                        f"disposable PostgreSQL did not become ready: {error}",
                        pytrace=False,
                    )
                time.sleep(0.1)
        yield admin_engine
    finally:
        if admin_engine is not None:
            admin_engine.dispose()
        _run(["docker", "stop", container], timeout=30, check=False)


@pytest.fixture
def postgres_engine(postgres_server_engine: Engine) -> Iterator[Engine]:
    database = postgres_database_name()
    with postgres_server_engine.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database}"')

    engine = create_engine(
        postgres_server_engine.url.set(database=database),
        pool_pre_ping=True,
    )
    try:
        yield engine
    finally:
        engine.dispose()
        with postgres_server_engine.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
