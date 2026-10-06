"""Test prerequisites: one marker registry and one policy for every suite.

A test that needs something beyond the locked Python environment carries a
prerequisite marker (``postgres``, ``needs_docker``, ``linux_only``, ...). When
the prerequisite is missing, the test skips locally with the reason, and fails
in CI when the CI runner provides that prerequisite (``CI_PROVIDED``) or the
job names it in the comma-separated ``VONK_CI_PREREQUISITES``. A skip is never
evidence, so CI must not silently skip what it is supposed to run.

Every test with a prerequisite marker is also marked ``lane``, so
``-m "not lane"`` selects the hermetic fast tier.

The repository, Controller and Compose conftests register this plugin from
``pytest_addoption``, like the time budget.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

# Loaded by path: the Controller suite cannot put the repository root (and its
# own ``tests`` package) on the import path.
_spec = importlib.util.spec_from_file_location(
    "vonk_cli_dependencies", Path(__file__).with_name("cli_dependencies.py")
)
assert _spec is not None and _spec.loader is not None
cli_dependencies = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli_dependencies)

_PLUGIN_NAME = "vonk-test-prerequisites"
_ROOT = Path(__file__).resolve().parents[1]

MARKERS = {
    "lane": "requires Docker, PostgreSQL, cargo or a Linux host tool; excluded from the fast tier",
    "linux_only": "requires Linux host semantics or Linux system tools",
    "needs_systemd": "requires systemd tooling (systemd-analyze)",
    "needs_dpkg_deb": "requires the Debian package builder at /usr/bin/dpkg-deb",
    "needs_dpkg": "requires the Debian version comparison tool at /usr/bin/dpkg",
    "needs_docker": "requires a working Docker daemon",
    "needs_buildx": "requires the Docker Buildx plugin",
    "needs_cargo": "requires the Rust toolchain",
    "needs_aptly": "requires aptly for package repository integration tests",
    "needs_recipe_library": "requires VONK_RECIPE_LIBRARY_ROOT to name the canonical recipe checkout",
    "needs_rust_probe": "drives the Linux-only Rust wire probes (scripts/tests/run_agent_wire_contracts.py)",
    "needs_repair_probe": "requires REPAIR_PROBE_BINARY or a built vonk-repair-helper-probe",
    "needs_installer_release_probe": "requires VONK_INSTALLER_RELEASE_WIRE_PROBE",
    "needs_agent_binary": "requires VONK_AGENT_BINARY to name a built vonk-agent binary",
    "needs_backup_container": "requires the opt-in PostgreSQL backup container lane (VONK_RUN_BACKUP_CONTAINER_TEST=1)",
    "needs_cli_dependencies": "requires the CLI dependency environment from scripts/sync-cli-dependencies",
    "postgres": "provisions a disposable PostgreSQL server through Docker (Linux lane)",
    "built_image": "checks a prebuilt Controller or worker image named by VONK_TEST_CONTROLLER_IMAGE or VONK_TEST_WORKER_IMAGE",
}

# What a GitHub Ubuntu runner running these suites provides. A dedicated job
# that provides more (aptly, a built agent binary, the backup container) opts
# in with VONK_CI_PREREQUISITES.
CI_PROVIDED = frozenset(
    {
        "linux_only",
        "needs_systemd",
        "needs_dpkg_deb",
        "needs_dpkg",
        "needs_docker",
        "needs_buildx",
        "needs_cargo",
        "needs_recipe_library",
        "needs_rust_probe",
        "needs_cli_dependencies",
        "postgres",
        "built_image",
    }
)


def register(pluginmanager: pytest.PytestPluginManager) -> None:
    if not pluginmanager.has_plugin(_PLUGIN_NAME):
        pluginmanager.register(sys.modules[__name__], _PLUGIN_NAME)


def _linux() -> str | None:
    return (
        None
        if sys.platform.startswith("linux")
        else f"Linux (running on {sys.platform})"
    )


def _tool(name: str) -> Callable[[], str | None]:
    return lambda: None if shutil.which(name) else name


def _file(path: str) -> Callable[[], str | None]:
    return lambda: None if Path(path).is_file() else path


def _executable_env(name: str) -> Callable[[], str | None]:
    def check() -> str | None:
        value = os.environ.get(name)
        if value and os.access(value, os.X_OK) and Path(value).is_file():
            return None
        return f"executable {name}"

    return check


def _command_succeeds(*command: str) -> bool:
    if shutil.which(command[0]) is None:
        return False
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _docker() -> str | None:
    return None if _command_succeeds("docker", "info") else "a working Docker daemon"


def _buildx() -> str | None:
    return None if _command_succeeds("docker", "buildx", "version") else "Docker Buildx"


def _systemd() -> str | None:
    return _linux() or (None if shutil.which("systemd-analyze") else "systemd-analyze")


def _recipe_library() -> str | None:
    configured = os.environ.get("VONK_RECIPE_LIBRARY_ROOT")
    if configured and (Path(configured) / "catalog-index.json").is_file():
        return None
    return (
        "VONK_RECIPE_LIBRARY_ROOT naming a recipe checkout with a built "
        "catalog-index.json (scripts/build-recipe-library)"
    )


def _repair_probe() -> str | None:
    if _executable_env("REPAIR_PROBE_BINARY")() is None:
        return None
    target = Path(os.environ.get("CARGO_TARGET_DIR", _ROOT / "target"))
    if (target / "release" / "vonk-repair-helper-probe").is_file():
        return None
    return "executable REPAIR_PROBE_BINARY"


def _installer_release_probe() -> str | None:
    configured = os.environ.get("VONK_INSTALLER_RELEASE_WIRE_PROBE")
    return (
        None
        if configured and Path(configured).is_file()
        else "VONK_INSTALLER_RELEASE_WIRE_PROBE"
    )


def _backup_container() -> str | None:
    if os.environ.get("VONK_RUN_BACKUP_CONTAINER_TEST") == "1":
        return None
    return "VONK_RUN_BACKUP_CONTAINER_TEST=1"


def _cli_dependencies() -> str | None:
    _, reason = cli_dependencies.site_packages(_ROOT)
    return None if reason is None else f"the CLI dependency environment ({reason})"


def _postgres() -> str | None:
    # Concurrent PostgreSQL and process-recovery tests run in the Linux lane.
    return _linux() or _docker()


def _built_image() -> str | None:
    if os.environ.get("VONK_TEST_CONTROLLER_IMAGE") or os.environ.get(
        "VONK_TEST_WORKER_IMAGE"
    ):
        return None
    return "VONK_TEST_CONTROLLER_IMAGE or VONK_TEST_WORKER_IMAGE (scripts/test-local --with-image-build)"


_CHECKS: dict[str, Callable[[], str | None]] = {
    "linux_only": _linux,
    "needs_systemd": _systemd,
    "needs_dpkg_deb": _file("/usr/bin/dpkg-deb"),
    "needs_dpkg": _file("/usr/bin/dpkg"),
    "needs_docker": _docker,
    "needs_buildx": _buildx,
    "needs_cargo": _tool("cargo"),
    "needs_aptly": _tool("aptly"),
    "needs_recipe_library": _recipe_library,
    "needs_rust_probe": _linux,
    "needs_repair_probe": _repair_probe,
    "needs_installer_release_probe": _installer_release_probe,
    "needs_agent_binary": _executable_env("VONK_AGENT_BINARY"),
    "needs_backup_container": _backup_container,
    "needs_cli_dependencies": _cli_dependencies,
    "postgres": _postgres,
    "built_image": _built_image,
}


@functools.cache
def missing(marker: str) -> str | None:
    """What is missing for ``marker`` in this session, or None when available."""

    return _CHECKS[marker]()


def _required_in_ci(marker: str) -> bool:
    if os.environ.get("CI", "").lower() != "true":
        return False
    extra = {
        name.strip()
        for name in os.environ.get("VONK_CI_PREREQUISITES", "").split(",")
        if name.strip()
    }
    return marker in CI_PROVIDED or marker in extra


def pytest_configure(config: pytest.Config) -> None:
    for name, description in MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {description}")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if any(item.get_closest_marker(name) for name in _CHECKS):
            item.add_marker(pytest.mark.lane)


def pytest_runtest_setup(item: pytest.Item) -> None:
    for name in _CHECKS:
        if item.get_closest_marker(name) is None:
            continue
        reason = missing(name)
        if reason is None:
            continue
        if _required_in_ci(name):
            pytest.fail(f"CI prerequisite missing for {name}: {reason}", pytrace=False)
        pytest.skip(f"{name}: requires {reason}")
