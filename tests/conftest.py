"""Repository-level test-lane taxonomy.

The fast tier runs only tests that need nothing beyond the Python environment.
A test that inspects Debian packages, drives a system OpenSSL, reads Linux
process state, or executes a shell/systemd suite is tagged ``lane`` and runs in
the container or designated CI lane instead. Tagging derives from the module's
own content, so a new host-tool test is classified without further edits.
"""

from __future__ import annotations

import os
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


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag every test that cannot run in the fast, hermetic tier."""

    for item in items:
        path = Path(str(item.fspath)).resolve()
        if path.suffix == ".py" and _module_is_lane(path):
            item.add_marker(pytest.mark.lane)
