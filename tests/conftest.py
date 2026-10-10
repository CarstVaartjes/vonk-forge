"""Repository suite configuration.

Prerequisite markers and their skip-locally/fail-in-CI policy live in
tools/pytest_prereqs.py, the per-test time budget in tools/pytest_budget.py;
both are shared with the Controller and Compose suites.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tools.generated_contracts import prepare_generated_contracts

prepare_generated_contracts()

from tools import pytest_budget, pytest_prereqs


def pytest_addoption(
    parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager
) -> None:
    pytest_budget.register(pluginmanager)
    pytest_prereqs.register(pluginmanager)


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


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Name the host prerequisites of the shell suites driven by one module."""

    for item in items:
        if Path(str(item.fspath)).name != "test_shell_suites.py":
            continue
        if "systemd" in item.nodeid:
            item.add_marker(pytest.mark.needs_systemd)
        if "test_agent_upgrade_repair_systemd.sh" in item.nodeid:
            item.add_marker(pytest.mark.needs_repair_probe)
