"""Per-test time budget for the repository, Controller and Compose suites.

Every test must finish its setup and body within ``DEFAULT_BUDGET_SECONDS``;
setting up a fixture shared beyond one test (session, package, module or
class scope) is excluded, because doing expensive work once is the point. A
test that genuinely needs longer carries ``@pytest.mark.slow(seconds)`` with a
comment naming why; ``MAX_BUDGET_SECONDS`` caps that exception.

The budget is checked after the test instead of interrupting it: an alarm that
fires at an arbitrary point can leave shared module state half-initialized and
fail unrelated tests later in the same worker. Hung tests are still stopped by
pytest-timeout's much longer ``timeout`` from the pytest configuration.

``--test-budget-scale`` multiplies every budget (``0`` disables the check) for
a machine that is knowingly overloaded; CI runs at the default scale.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Generator

import pytest

DEFAULT_BUDGET_SECONDS = 10.0
MAX_BUDGET_SECONDS = 60.0

_SETUP_DURATION = pytest.StashKey[float]()
_PLUGIN_NAME = "vonk-test-budget"


def register(pluginmanager: pytest.PytestPluginManager) -> None:
    """Register once from a conftest's ``pytest_addoption``.

    Each suite's conftest calls this so the budget applies however the suite
    is invoked; a run that loads several of those conftests registers it once.
    """

    if not pluginmanager.has_plugin(_PLUGIN_NAME):
        pluginmanager.register(sys.modules[__name__], _PLUGIN_NAME)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--test-budget-scale",
        type=float,
        default=1.0,
        help="multiply every per-test time budget; 0 disables the check",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "slow(seconds): the test may take up to `seconds` (at most "
        f"{MAX_BUDGET_SECONDS:g}) instead of {DEFAULT_BUDGET_SECONDS:g}; "
        "justify it in a comment",
    )


def budget_seconds(item: pytest.Item) -> float:
    marker = item.get_closest_marker("slow")
    if marker is None:
        return DEFAULT_BUDGET_SECONDS
    if len(marker.args) != 1 or marker.kwargs:
        raise pytest.UsageError(f"{item.nodeid}: use @pytest.mark.slow(<seconds>)")
    seconds = float(marker.args[0])
    if not DEFAULT_BUDGET_SECONDS < seconds <= MAX_BUDGET_SECONDS:
        raise pytest.UsageError(
            f"{item.nodeid}: slow budget {seconds:g}s must be above "
            f"{DEFAULT_BUDGET_SECONDS:g}s and at most {MAX_BUDGET_SECONDS:g}s"
        )
    return seconds


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        budget_seconds(item)


class _SharedSetup:
    """Time spent creating fixtures shared beyond one test.

    A session- or module-scoped fixture (a database server, an installed CLI,
    pulled base images) is set up once and reused, which is exactly what a
    slow test should do. Its one-off cost is not charged to whichever test
    happens to request it first.
    """

    depth = 0
    seconds = 0.0

    @classmethod
    def take(cls) -> float:
        seconds, cls.seconds = cls.seconds, 0.0
        return seconds


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(
    fixturedef: pytest.FixtureDef[object], request: pytest.FixtureRequest
) -> Generator[None, object, object]:
    if fixturedef.scope == "function":
        return (yield)
    _SharedSetup.depth += 1
    started = time.perf_counter()
    try:
        return (yield)
    finally:
        _SharedSetup.depth -= 1
        if _SharedSetup.depth == 0:
            _SharedSetup.seconds += time.perf_counter() - started


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    own_duration = max(0.0, report.duration - _SharedSetup.take())
    if report.when == "setup":
        item.stash[_SETUP_DURATION] = own_duration
        return report
    if report.when != "call" or not report.passed:
        return report
    scale = item.config.getoption("--test-budget-scale")
    if not scale:
        return report
    budget = budget_seconds(item) * scale
    elapsed = item.stash.get(_SETUP_DURATION, 0.0) + own_duration
    if elapsed > budget:
        report.outcome = "failed"
        report.longrepr = (
            f"test took {elapsed:.1f}s (setup and call, excluding shared "
            f"session fixtures), over its {budget:g}s budget; make it faster, "
            "or mark it @pytest.mark.slow(<seconds>) with a comment saying why "
            "(see docs/testing-and-ci.md)"
        )
    return report
