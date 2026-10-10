"""Per-test time budget for the repository, Controller and Compose suites.

The plugin measures setup and body time against a calibrated reporting
threshold. Shared fixture setup is excluded. ``@pytest.mark.slow(seconds)``
declares a higher threshold, up to ``MAX_BUDGET_SECONDS``.

Overruns appear in the ``slow tests`` terminal summary and JUnit properties.
They never fail or rerun a test or change the run's exit status. Workflow job
limits and pytest-timeout remain the guards against hangs.

A short CPU benchmark scales thresholds for slow or loaded hosts.
``--test-budget-scale`` multiplies them further (``0`` disables reporting);
``VONK_TEST_BUDGET_CALIBRATION`` pins the calibration.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Generator

import pytest

DEFAULT_BUDGET_SECONDS = 10.0
MAX_BUDGET_SECONDS = 60.0

#: The slowest host the calibration will compensate for; beyond it the host,
#: not the test, is the problem.
MAX_CALIBRATION = 5.0
#: Median time of ``_benchmark_workload`` on the machine the budgets were set on.
REFERENCE_BENCHMARK_SECONDS = 0.105
CALIBRATION_ENV = "VONK_TEST_BUDGET_CALIBRATION"

_SETUP_DURATION = pytest.StashKey[float]()
_CALIBRATION = pytest.StashKey[float]()
_OVERRUN_PROPERTY = "vonk-test-budget-overrun"
_OVERRUNS: dict[str, str] = {}
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
        help="multiply every per-test time budget; 0 disables reporting",
    )


def _benchmark_workload() -> int:
    """A fixed, allocation- and dict-heavy pure-Python loop, like the scanners."""

    table: dict[str, int] = {}
    for number in range(300_000):
        table[f"key-{number % 997}"] = table.get(f"key-{number % 991}", 0) + number
    return len(table)


def benchmark_seconds(rounds: int = 3) -> float:
    """Median wall time of the benchmark, long enough to be descheduled.

    Wall time, not CPU time: CPU contention on a loaded host is what slows the
    tests, and only wall time sees it. The median, not the minimum: the fastest
    sample is the least disturbed one and would hide exactly that load."""

    samples: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        _benchmark_workload()
        samples.append(time.perf_counter() - started)
    return sorted(samples)[len(samples) // 2]


def calibration_factor(
    measured: float, reference: float = REFERENCE_BENCHMARK_SECONDS
) -> float:
    """How many times slower than the reference machine, between 1 and the cap."""

    if measured <= 0 or reference <= 0:
        return 1.0
    return min(MAX_CALIBRATION, max(1.0, measured / reference))


def session_calibration() -> float:
    pinned = os.environ.get(CALIBRATION_ENV, "")
    if pinned:
        return max(1.0, float(pinned))
    return calibration_factor(benchmark_seconds())


def pytest_configure(config: pytest.Config) -> None:
    _OVERRUNS.clear()
    if config.getoption("--test-budget-scale", 1.0):
        config.stash[_CALIBRATION] = session_calibration()
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
    budget = budget_seconds(item) * scale * item.config.stash.get(_CALIBRATION, 1.0)
    elapsed = item.stash.get(_SETUP_DURATION, 0.0) + own_duration
    if elapsed <= budget:
        return report
    message = (
        f"test took {elapsed:.1f}s (setup and call, excluding shared "
        f"session fixtures), over its {budget:g}s budget (scaled by the runner "
        "calibration); make it faster, or mark it @pytest.mark.slow(<seconds>) "
        "with a comment saying why (see docs/testing-and-ci.md)"
    )
    item.user_properties.append((_OVERRUN_PROPERTY, message))
    report.user_properties.append((_OVERRUN_PROPERTY, message))
    return report


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    # Also runs in the xdist controller, which receives the worker's properties.
    for name, value in report.user_properties:
        if name == _OVERRUN_PROPERTY:
            _OVERRUNS[report.nodeid] = str(value)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if _OVERRUNS:
        terminalreporter.section("slow tests")
        for nodeid, message in sorted(_OVERRUNS.items()):
            terminalreporter.line(f"{nodeid}: {message}")
