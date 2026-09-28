from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .api_response_witness import (
    pytest_addoption as _api_response_addoption,
)
from .api_response_witness import (
    pytest_configure as _api_response_pytest_configure,
)
from .api_response_witness import (
    pytest_runtest_setup as _api_response_runtest_setup,
)
from .api_response_witness import (  # noqa: F401 - pytest discovers imported hooks.
    pytest_runtest_teardown,
    pytest_terminal_summary,
)
from .api_response_witness import (
    pytest_sessionfinish as _api_response_sessionfinish,
)

POSTGRES_IMAGE = "postgres:18.3"
_POSTGRES_PASSWORD = "postgres"
_POSTGRES_OWNER_LABEL = "dev.vonk-forge.control-tests.owner"
_POSTGRES_PORT_TEMPLATE = (
    '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}'
)

_REGISTERED_MARKERS = (
    "linux_only: requires a Linux operating system or Linux container behavior",
    "needs_dpkg_deb: requires the Debian package builder at /usr/bin/dpkg-deb",
    "needs_buildx: requires the Docker Buildx plugin for image builds",
    "needs_systemd: requires systemd tools or a systemd host",
    "needs_recipe_library: requires VONK_RECIPE_LIBRARY_ROOT to name the canonical recipe checkout",
    "needs_rust_probe: requires Rust wire probes built by scripts/tests/run_agent_wire_contracts.py",
    "needs_uv_cache: requires cached wheels for offline installed-CLI tests",
    "postgres: provisions a disposable PostgreSQL server through Docker",
)


def _load_budget_plugin():
    """Load the shared per-test budget without exposing the repository root.

    The repository root also has a ``tests`` package, so it cannot go on this
    suite's import path.
    """

    name = "vonk_pytest_budget"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[2] / "tools" / "pytest_budget.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def pytest_addoption(
    parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager
) -> None:
    _api_response_addoption(parser)
    _load_budget_plugin().register(pluginmanager)


def pytest_configure(config: pytest.Config) -> None:
    _api_response_pytest_configure(config)
    for marker in _REGISTERED_MARKERS:
        config.addinivalue_line("markers", marker)


@pytest.fixture(scope="session", autouse=True)
def hermetic_git_config() -> Iterator[None]:
    """Keep a developer's global git config out of throwaway test repositories.

    Tests that ``git init`` a temporary repository must behave the same on a
    laptop as in CI. Without this, global settings such as ``commit.gpgsign``,
    ``tag.gpgsign``, hooks, or ``init.defaultBranch`` leak into fixtures and
    turn an environment preference into a test error.
    """

    overrides = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    previous = {name: os.environ.get(name) for name in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def postgres_database_name() -> str:
    return f"vonk_test_{uuid.uuid4().hex}"


def _docker_unavailable(message: str) -> None:
    if os.getenv("CI", "").lower() == "true":
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


class _PostgresServer:
    """One disposable PostgreSQL server per test process.

    It starts when collection finishes, and only if a selected test needs it,
    so container start-up and ``initdb`` are not charged to whichever test
    happens to request the fixture first. The data directory is a tmpfs and
    durability is off: the server is discarded after the session, and tests
    that kill client processes rely on committed rows, not on crash safety of
    the server itself.
    """

    container: str | None = None
    engine: Engine | None = None
    unavailable: str | None = None

    @classmethod
    def start(cls) -> None:
        if shutil.which("docker") is None:
            cls.unavailable = "Docker is required for PostgreSQL integration tests"
            return
        try:
            docker_info = _run(["docker", "info"], timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            cls.unavailable = f"Docker is unavailable: {error}"
            return
        if docker_info.returncode != 0:
            detail = docker_info.stderr.strip() or docker_info.stdout.strip()
            cls.unavailable = f"Docker is unavailable: {detail}"
            return
        cls._stop_orphans()
        try:
            started = _run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-d",
                    "--name",
                    f"vonk-control-tests-{uuid.uuid4().hex[:12]}",
                    "--label",
                    f"{_POSTGRES_OWNER_LABEL}={socket.gethostname()}:{os.getpid()}",
                    "-e",
                    f"POSTGRES_PASSWORD={_POSTGRES_PASSWORD}",
                    "--tmpfs",
                    "/var/lib/postgresql",
                    "-p",
                    "127.0.0.1::5432",
                    POSTGRES_IMAGE,
                    "-c",
                    "fsync=off",
                    "-c",
                    "synchronous_commit=off",
                    "-c",
                    "full_page_writes=off",
                ],
                timeout=180,
            )
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            cls.unavailable = f"disposable PostgreSQL failed to start: {error}"
            return
        cls.container = started.stdout.strip()
        inspected = _run(
            ["docker", "inspect", "-f", _POSTGRES_PORT_TEMPLATE, cls.container],
            timeout=15,
        )
        port = inspected.stdout.strip()
        engine = create_engine(
            "postgresql+psycopg://"
            f"postgres:{_POSTGRES_PASSWORD}@127.0.0.1:{port}/postgres",
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=True,
        )
        deadline = time.monotonic() + 60
        while True:
            try:
                with engine.connect():
                    break
            except (OSError, SQLAlchemyError) as error:
                if time.monotonic() >= deadline:
                    engine.dispose()
                    cls.unavailable = (
                        f"disposable PostgreSQL did not become ready: {error}"
                    )
                    return
                time.sleep(0.1)
        cls.engine = engine

    @classmethod
    def _stop_orphans(cls) -> None:
        """Stop servers whose test process died without its session teardown."""

        listed = _run(
            [
                "docker",
                "ps",
                "--filter",
                f"label={_POSTGRES_OWNER_LABEL}",
                "--format",
                f'{{{{.ID}}}} {{{{.Label "{_POSTGRES_OWNER_LABEL}"}}}}',
            ],
            timeout=15,
            check=False,
        )
        orphans = []
        for line in listed.stdout.splitlines():
            container, _, owner = line.partition(" ")
            host, _, pid = owner.rpartition(":")
            if host != socket.gethostname() or not pid.isdigit():
                continue
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                orphans.append(container)
            except PermissionError:
                continue
        if orphans:
            _run(["docker", "stop", *orphans], timeout=60, check=False)

    @classmethod
    def stop(cls) -> None:
        if cls.engine is not None:
            cls.engine.dispose()
            cls.engine = None
        if cls.container is not None:
            _run(["docker", "stop", cls.container], timeout=30, check=False)
            cls.container = None


def pytest_collection_finish(session: pytest.Session) -> None:
    if any(item.get_closest_marker("postgres") for item in session.items) and (
        sys.platform.startswith("linux")
    ):
        _PostgresServer.start()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _PostgresServer.stop()
    _api_response_sessionfinish(session, exitstatus)


@pytest.fixture(scope="session")
def postgres_server_engine() -> Engine:
    if _PostgresServer.engine is None:
        _docker_unavailable(
            _PostgresServer.unavailable or "PostgreSQL was not started for this session"
        )
    assert _PostgresServer.engine is not None
    return _PostgresServer.engine


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


# Test-lane taxonomy.
#
# The fast tier runs only tests that need nothing beyond the Python
# environment.  Anything that starts Docker, creates a PostgreSQL cluster,
# builds a Rust probe or drives a Linux host tool is tagged ``lane`` and runs
# in the container or designated CI lane instead.  PostgreSQL fixtures and
# ``*_wire_bridge.py`` Rust probes are inferred from what a test requests.  A
# test that shells out to Docker carries an explicit ``@pytest.mark.lane`` in
# the module next to the Docker call, so the reason stays visible where the
# container starts and a module is never tagged wholesale.

_POSTGRES_FIXTURES = frozenset({"postgres_engine", "postgres_server_engine"})


def _is_ci() -> bool:
    return os.getenv("CI", "").lower() == "true"


def _missing_prerequisite(marker: str, item: pytest.Item) -> str | None:
    if marker == "linux_only" and not sys.platform.startswith("linux"):
        return f"{marker} tests require Linux (current platform: {sys.platform})"
    if marker == "needs_dpkg_deb" and not Path("/usr/bin/dpkg-deb").is_file():
        return "needs_dpkg_deb tests require /usr/bin/dpkg-deb"
    if marker == "needs_systemd":
        if not sys.platform.startswith("linux"):
            return f"{marker} tests require Linux (current platform: {sys.platform})"
        if shutil.which("systemd-analyze") is None:
            return "needs_systemd tests require systemd-analyze"
    if marker == "needs_buildx":
        if shutil.which("docker") is None:
            return "needs_buildx tests require Docker and its Buildx plugin"
        buildx = subprocess.run(
            ["docker", "buildx", "version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if buildx.returncode != 0:
            return "needs_buildx tests require the Docker Buildx plugin"
    if marker == "needs_recipe_library":
        configured = os.environ.get("VONK_RECIPE_LIBRARY_ROOT")
        if not configured or not (Path(configured) / "catalog-index.json").is_file():
            return "needs_recipe_library tests require VONK_RECIPE_LIBRARY_ROOT with catalog-index.json"
    if marker == "needs_rust_probe" and not sys.platform.startswith("linux"):
        return "needs_rust_probe tests drive Linux-only agent wire probes"
    if marker == "postgres":
        if not sys.platform.startswith("linux"):
            return (
                "postgres tests require the Linux Docker lane for reliable concurrent "
                "PostgreSQL/process recovery"
            )
        if shutil.which("docker") is None:
            return "postgres tests require Docker to provision a disposable PostgreSQL server"
    return None


_PREREQUISITE_MARKERS = (
    "linux_only",
    "needs_dpkg_deb",
    "needs_systemd",
    "needs_buildx",
    "needs_recipe_library",
    "needs_rust_probe",
    "needs_uv_cache",
    "postgres",
)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Skip unavailable integration lanes locally and fail loudly in CI."""

    _api_response_runtest_setup(item)
    for marker in _PREREQUISITE_MARKERS:
        if item.get_closest_marker(marker) is None:
            continue
        reason = _missing_prerequisite(marker, item)
        if reason is None:
            continue
        if _is_ci():
            pytest.fail(f"CI prerequisite missing: {reason}", pytrace=False)
        pytest.skip(reason)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag every test that cannot run in the fast, hermetic tier."""

    for item in items:
        if isinstance(item, pytest.Function) and _POSTGRES_FIXTURES.intersection(
            item.fixturenames
        ):
            item.add_marker(pytest.mark.postgres)
        if (
            isinstance(item, pytest.Function)
            and "installed_vonkctl" in item.fixturenames
        ):
            item.add_marker(pytest.mark.needs_uv_cache)
        if Path(str(item.fspath)).name.endswith("_wire_bridge.py"):
            item.add_marker(pytest.mark.needs_rust_probe)
        module_namespace = getattr(getattr(item, "module", None), "__dict__", {})
        if "recipe_library_root" in module_namespace:
            item.add_marker(pytest.mark.needs_recipe_library)
        if any(
            item.get_closest_marker(marker)
            for marker in (
                "postgres",
                "needs_rust_probe",
                "needs_uv_cache",
                "needs_recipe_library",
                "needs_systemd",
                "needs_buildx",
                "linux_only",
                "needs_dpkg_deb",
            )
        ):
            item.add_marker(pytest.mark.lane)
