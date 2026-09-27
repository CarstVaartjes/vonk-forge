"""Repository-level test-lane taxonomy.

The fast tier runs only tests that need nothing beyond the Python environment.
A test that inspects Debian packages, drives a system OpenSSL, reads Linux
process state, or executes a shell/systemd suite is tagged ``lane`` and runs in
the container or designated CI lane instead. Tagging derives from the module's
own content, so a new host-tool test is classified without further edits.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest


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


# A module containing any of these drives something the host must provide.
_LANE_SIGNALS = (
    "/usr/bin/dpkg",
    "dpkg-deb",
    "/usr/bin/openssl",
    "/proc/",
    "stat -c",
)

# Modules that need a Linux lane for reasons their source text does not spell
# out: a pty-driven runner with Linux process accounting, the shell/systemd
# suite driver, and the Debian package-state inspector.
_LANE_MODULES = frozenset(
    {
        "test_acceptance_runtime.py",
        "test_agent_apt_state.py",
        "test_shell_suites.py",
    }
)

_source_is_lane: dict[Path, bool] = {}
_docker_available: bool | None = None


def _module_is_lane(path: Path) -> bool:
    if path.name in _LANE_MODULES or path.parent.name == "nodes":
        return True
    cached = _source_is_lane.get(path)
    if cached is None:
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            text = ""
        cached = any(signal in text for signal in _LANE_SIGNALS)
        _source_is_lane[path] = cached
    return cached


def _docker_daemon_available() -> bool:
    global _docker_available
    if _docker_available is None:
        docker = shutil.which("docker")
        if docker is None:
            _docker_available = False
        else:
            import subprocess

            try:
                result = subprocess.run(
                    [docker, "info", "--format", "{{.ServerVersion}}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                _docker_available = False
            else:
                _docker_available = result.returncode == 0 and bool(
                    result.stdout.strip()
                )
    return _docker_available


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag every test that cannot run in the fast, hermetic tier."""

    for item in items:
        path = Path(str(item.fspath)).resolve()
        if path.suffix == ".py" and _module_is_lane(path):
            item.add_marker(pytest.mark.lane)

        if path.name == "test_shell_suites.py" and "systemd" in item.nodeid:
            item.add_marker(pytest.mark.needs_systemd)
        if (
            path.name == "test_shell_suites.py"
            and "test_agent_upgrade_repair_systemd.sh" in item.nodeid
        ):
            item.add_marker(pytest.mark.needs_rust_probe)

        missing: list[str] = []
        if item.get_closest_marker("linux_only") and sys.platform != "linux":
            missing.append("Linux host semantics")
        if item.get_closest_marker("needs_systemd") and not shutil.which(
            "systemd-analyze"
        ):
            missing.append("systemd-analyze")
        if item.get_closest_marker("needs_dpkg_deb") and not (
            Path("/usr/bin/dpkg-deb").is_file()
        ):
            missing.append("dpkg-deb")
        if item.get_closest_marker("needs_dpkg") and not (
            Path("/usr/bin/dpkg").is_file()
        ):
            missing.append("dpkg")
        if item.get_closest_marker("needs_cargo") and not shutil.which("cargo"):
            missing.append("cargo")
        if item.get_closest_marker("needs_aptly") and not shutil.which("aptly"):
            missing.append("aptly")
        if (
            item.get_closest_marker("needs_backup_container")
            and os.environ.get("VONK_RUN_BACKUP_CONTAINER_TEST") != "1"
        ):
            missing.append("VONK_RUN_BACKUP_CONTAINER_TEST=1")
        if item.get_closest_marker("needs_docker") and not _docker_daemon_available():
            missing.append("available Docker daemon")
        if item.get_closest_marker("needs_recipe_library"):
            library_root = os.environ.get("VONK_RECIPE_LIBRARY_ROOT")
            if not library_root or not Path(library_root).is_dir():
                missing.append("VONK_RECIPE_LIBRARY_ROOT pointing to a checkout")
        if item.get_closest_marker("needs_rust_probe"):
            probe = os.environ.get("REPAIR_PROBE_BINARY")
            cargo_target = os.environ.get("CARGO_TARGET_DIR")
            repository_root = Path(__file__).resolve().parents[1]
            built_probe = (
                Path(cargo_target) if cargo_target else repository_root / "target"
            )
            built_probe = built_probe / "release" / "vonk-repair-helper-probe"
            probe_exists = probe and Path(probe).is_file() and os.access(probe, os.X_OK)
            if not probe_exists and not built_probe.is_file():
                missing.append("executable REPAIR_PROBE_BINARY")
        if item.get_closest_marker("needs_installer_release_probe"):
            release_probe = os.environ.get("VONK_INSTALLER_RELEASE_WIRE_PROBE")
            if not release_probe or not Path(release_probe).is_file():
                missing.append("VONK_INSTALLER_RELEASE_WIRE_PROBE")
        if missing:
            reason = "requires " + ", ".join(missing)
            if os.environ.get("CI", "").lower() == "true":
                raise pytest.UsageError(f"{item.nodeid}: {reason} (CI must provide it)")
            item.add_marker(pytest.mark.skip(reason=reason))
