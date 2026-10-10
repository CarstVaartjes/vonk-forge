from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from vonk_control.stored_json import install_write_guard

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

# A test that writes a JSON column its contract refuses fails where it writes;
# one that damages a row on purpose says so with ``write_guard_mode(strict=False)``.
install_write_guard(strict=True)

# The same pinned PostgreSQL the Compose deployment runs; scripts/pull-test-images
# pulls it before the suite, so starting the server never downloads.
POSTGRES_IMAGE = json.loads(
    (Path(__file__).resolve().parents[2] / "deploy/compose/images.lock.json").read_text(
        encoding="utf-8"
    )
)["images"]["postgres"]
_POSTGRES_PASSWORD = "postgres"
_POSTGRES_OWNER_LABEL = "dev.vonk-forge.control-tests.owner"
_POSTGRES_NAME_PREFIX = "vonk-control-tests-"
_POSTGRES_STALE_AFTER = timedelta(hours=1)
_POSTGRES_PORT_TEMPLATE = (
    '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}'
)


def _load_tools_plugin(name: str):
    """Load a shared tools/ pytest plugin without exposing the repository root.

    The repository root also has a ``tests`` package, so it cannot go on this
    suite's import path.
    """

    module_name = f"vonk_{name}"
    if module_name not in sys.modules:
        path = Path(__file__).resolve().parents[2] / "tools" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[module_name]


@pytest.fixture
def damaged_json_rows() -> Iterator[None]:
    """Let a test write JSON documents its contract refuses (damaged or retired rows)."""

    from vonk_control.stored_json import write_guard_mode

    with write_guard_mode(strict=False):
        yield


_load_tools_plugin("generated_contracts").prepare_generated_contracts()


def pytest_addoption(
    parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager
) -> None:
    _api_response_addoption(parser)
    _load_tools_plugin("pytest_budget").register(pluginmanager)
    _load_tools_plugin("pytest_prereqs").register(pluginmanager)


def pytest_configure(config: pytest.Config) -> None:
    _api_response_pytest_configure(config)


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


def _session_owner_pid() -> int:
    """The process that owns this pytest session and its PostgreSQL server.

    xdist workers are children of the controller process, which outlives them
    and runs the final session teardown.
    """

    return os.getppid() if os.environ.get("PYTEST_XDIST_WORKER") else os.getpid()


def _session_state(owner: int) -> Path:
    return Path(tempfile.gettempdir()) / (
        f"vonk-control-postgres-{socket.gethostname()}-{owner}.json"
    )


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _PostgresServer:
    """One disposable PostgreSQL server per pytest session.

    The first xdist worker that needs it starts it when collection finishes;
    the others attach to the same server, and each test still gets its own
    database. Start-up and ``initdb`` therefore happen once and are not charged
    to whichever test first requests the fixture. The session owner (the xdist
    controller, or the only process) stops it at session end.

    The data directory is a tmpfs and durability is off: the server is
    discarded after the session, and tests that kill client processes rely on
    committed rows, not on crash safety of the server itself. The next
    session stops a server whose owning session died without teardown, and
    an unlabelled test server (from before owner labels) older than
    ``_POSTGRES_STALE_AFTER``.
    """

    engine: Engine | None = None
    unavailable: str | None = None

    @classmethod
    def attach(cls) -> None:
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
        owner = _session_owner_pid()
        state = _session_state(owner)
        with open(state.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _stop_stale_servers()
            port = cls._running_port(state)
            if port is None:
                try:
                    port = cls._start(owner, state)
                except (
                    OSError,
                    subprocess.CalledProcessError,
                    subprocess.TimeoutExpired,
                ) as error:
                    cls.unavailable = f"disposable PostgreSQL failed to start: {error}"
                    return
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

    @staticmethod
    def _running_port(state: Path) -> str | None:
        try:
            recorded = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        running = _run(
            ["docker", "inspect", "-f", "{{.State.Running}}", recorded["container"]],
            timeout=15,
            check=False,
        )
        return recorded["port"] if running.stdout.strip() == "true" else None

    @staticmethod
    def _start(owner: int, state: Path) -> str:
        started = _run(
            [
                "docker",
                "run",
                "--rm",
                "-d",
                "--name",
                f"{_POSTGRES_NAME_PREFIX}{uuid.uuid4().hex[:12]}",
                "--label",
                f"{_POSTGRES_OWNER_LABEL}={socket.gethostname()}:{owner}",
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
        container = started.stdout.strip()
        port = _run(
            ["docker", "inspect", "-f", _POSTGRES_PORT_TEMPLATE, container],
            timeout=15,
        ).stdout.strip()
        state.write_text(
            json.dumps({"container": container, "port": port}), encoding="utf-8"
        )
        return port

    @classmethod
    def release(cls) -> None:
        if cls.engine is not None:
            cls.engine.dispose()
            cls.engine = None
        if os.environ.get("PYTEST_XDIST_WORKER"):
            return
        state = _session_state(os.getpid())
        try:
            recorded = json.loads(state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        _run(["docker", "stop", recorded["container"]], timeout=30, check=False)
        state.unlink(missing_ok=True)
        state.with_suffix(".lock").unlink(missing_ok=True)


def _stop_stale_servers() -> None:
    """Stop test servers that no live session owns."""

    listed = _run(
        [
            "docker",
            "ps",
            "--filter",
            f"name={_POSTGRES_NAME_PREFIX}",
            "--format",
            f'{{{{.ID}}}}\t{{{{.CreatedAt}}}}\t{{{{.Label "{_POSTGRES_OWNER_LABEL}"}}}}',
        ],
        timeout=15,
        check=False,
    )
    now = datetime.now(UTC)
    stale = []
    for line in listed.stdout.splitlines():
        container, created_at, owner = (line.split("\t") + ["", ""])[:3]
        try:
            created = datetime.strptime(created_at[:25], "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            created = now
        host, _, pid = owner.rpartition(":")
        if pid.isdigit():
            # A labelled server is in use exactly while its session lives.
            if host == socket.gethostname() and not _process_is_alive(int(pid)):
                stale.append(container)
        elif now - created > _POSTGRES_STALE_AFTER:
            # Unlabelled servers predate owner labels; only age tells.
            stale.append(container)
    if stale:
        _run(["docker", "stop", *stale], timeout=120, check=False)


def pytest_collection_finish(session: pytest.Session) -> None:
    if any(item.get_closest_marker("postgres") for item in session.items) and (
        sys.platform.startswith("linux")
    ):
        _PostgresServer.attach()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _PostgresServer.release()
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


# Prerequisite markers the suite infers from what a test requests. Their
# skip-locally/fail-in-CI policy and the ``lane`` marker come from
# tools/pytest_prereqs.py. A test that shells out to Docker carries its marker
# in the module next to the Docker call.

_POSTGRES_FIXTURES = frozenset({"postgres_engine", "postgres_server_engine"})


def pytest_runtest_setup(item: pytest.Item) -> None:
    _api_response_runtest_setup(item)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if isinstance(item, pytest.Function) and _POSTGRES_FIXTURES.intersection(
            item.fixturenames
        ):
            item.add_marker(pytest.mark.postgres)
        if (
            isinstance(item, pytest.Function)
            and "installed_vonkctl" in item.fixturenames
        ):
            item.add_marker(pytest.mark.needs_cli_dependencies)
            # Installed-CLI tests are process-boundary tests by definition: each
            # runs vonkctl several times as a separate process against a real
            # HTTPS Controller peer. They get one shared, explicit allowance.
            if item.get_closest_marker("slow") is None:
                item.add_marker(pytest.mark.slow(20))
        if Path(str(item.fspath)).name.endswith("_wire_bridge.py"):
            item.add_marker(pytest.mark.needs_rust_probe)
        module_namespace = getattr(getattr(item, "module", None), "__dict__", {})
        if "recipe_library_root" in module_namespace:
            item.add_marker(pytest.mark.needs_recipe_library)


@pytest.fixture(scope="session")
def parsed_repository() -> None:
    """Parse the scanned source trees once, outside any one test's time budget.

    The repository scanners share the parse (``parsed_sources``); requesting this
    fixture makes the first scanner test pay nothing for it."""

    from .parsed_sources import parse_file, python_files
    from .vocabulary_literals import CODE_POSITION_ROOT, PYTHON_ROOTS, REPO_ROOT

    for root in {*PYTHON_ROOTS, CODE_POSITION_ROOT}:
        for module in python_files(REPO_ROOT / root):
            parse_file(module)
