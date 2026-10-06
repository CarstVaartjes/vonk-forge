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

A budget is wall-clock time on a reference machine, so it is scaled by a
calibration measured at session start (a short CPU benchmark; see
``calibration_factor``): a slow or loaded host gets proportionally more time.
``--test-budget-scale`` multiplies every budget on top (``0`` disables the
check); ``VONK_TEST_BUDGET_CALIBRATION`` pins the calibration.

One overrun never fails a run, because a shared runner can stall for a moment.
A test over its budget is run once more alone in a fresh process: it fails only
when the overrun reproduces there, or when it is beyond ``FAR_BEYOND_BUDGET``
times its budget (no noise explains that). Otherwise the run reports a
warning (summary line and ``::warning::`` annotation).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Generator

import pytest

DEFAULT_BUDGET_SECONDS = 10.0
MAX_BUDGET_SECONDS = 60.0

#: A test this many times over its budget fails without a rerun.
FAR_BEYOND_BUDGET = 5.0
#: The slowest host the calibration will compensate for; beyond it the host,
#: not the test, is the problem.
MAX_CALIBRATION = 5.0
#: Best-of-N time of ``_benchmark_workload`` on the machine the budgets were set on.
REFERENCE_BENCHMARK_SECONDS = 0.105
CALIBRATION_ENV = "VONK_TEST_BUDGET_CALIBRATION"
_ISOLATED_ENV = "VONK_TEST_BUDGET_ISOLATED_RERUN"

_SETUP_DURATION = pytest.StashKey[float]()
_CALIBRATION = pytest.StashKey[float]()
#: Longest the one isolated rerun of every suspect may take.
RERUN_TIMEOUT_SECONDS = 900.0
_SUSPECT_PROPERTY = "vonk-test-budget-suspect"
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
    if elapsed > FAR_BEYOND_BUDGET * budget:
        report.outcome = "failed"
        report.longrepr = (
            f"{message}. {elapsed / budget:.1f}x its budget: no runner noise "
            "explains that"
        )
        return report
    if _isolated():
        report.outcome = "failed"
        report.longrepr = f"{message}. Reproduced when run alone."
        return report
    # One overrun is not evidence. The session end runs the suspects again,
    # alone; running them here would sit inside pytest-timeout's hang guard.
    report.user_properties.append((_SUSPECT_PROPERTY, f"{item.nodeid}\t{message}"))
    return report


def _isolated() -> bool:
    return bool(os.environ.get(_ISOLATED_ENV))


_REPRODUCED = re.compile(r"^FAILED (\S+) - test took ([0-9.]+)s ", re.MULTILINE)


def _plugin_arguments(arguments: tuple[str, ...] | list[str]) -> list[str]:
    """The ``-p <plugin>`` options of the original command line, for the rerun."""

    found: list[str] = []
    for position, argument in enumerate(arguments):
        if argument == "-p" and position + 1 < len(arguments):
            found += ["-p", arguments[position + 1]]
        elif argument.startswith("-p") and len(argument) > 2:
            found.append(argument)
    return found


def rerun_alone(config: pytest.Config, nodeids: list[str]) -> dict[str, str | None]:
    """Run ``nodeids`` again in one fresh process, without the other tests.

    Each result is ``None`` when the test did not go over its budget again, or
    the reproduced duration."""

    scale = config.getoption("--test-budget-scale")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-rf",
        "-p",
        "no:cacheprovider",
        f"--test-budget-scale={scale}",
        "--rootdir",
        str(config.rootpath),
    ]
    if config.inipath is not None:
        command += ["-c", str(config.inipath)]
    command += _plugin_arguments(config.invocation_params.args)
    command += nodeids
    try:
        finished = subprocess.run(
            command,
            cwd=config.rootpath,
            env={**os.environ, _ISOLATED_ENV: "1"},
            capture_output=True,
            text=True,
            check=False,
            timeout=RERUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {nodeid: "did not finish in time" for nodeid in nodeids}
    reproduced = {
        match.group(1): f"{match.group(2)}s"
        for match in _REPRODUCED.finditer(finished.stdout)
    }
    return {nodeid: reproduced.get(nodeid) for nodeid in nodeids}


_SUSPECTS: dict[str, str] = {}
_REPRODUCED_OVERRUNS: list[str] = []
_NOISE: list[str] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    # Also runs in the xdist controller, which receives the worker's properties.
    for name, value in report.user_properties:
        if name == _SUSPECT_PROPERTY:
            nodeid, _, message = str(value).partition("\t")
            _SUSPECTS[nodeid] = message


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Run every one-off overrun again alone; only a reproduced one fails.

    Runs once, in the controller, after the session's own fixtures are torn
    down, so the rerun cannot collide with them (a second PostgreSQL server)."""

    if hasattr(session.config, "workerinput") or not _SUSPECTS:
        return
    results = rerun_alone(session.config, sorted(_SUSPECTS))
    for nodeid, message in sorted(_SUSPECTS.items()):
        again = results.get(nodeid)
        if again is None:
            _NOISE.append(
                f"{nodeid}: {message}. Not reproduced when run alone; "
                "treated as runner noise, warning only"
            )
        else:
            _REPRODUCED_OVERRUNS.append(
                f"{nodeid}: {message}. Reproduced when run alone ({again})"
            )
    if _REPRODUCED_OVERRUNS and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if _REPRODUCED_OVERRUNS:
        terminalreporter.section("tests over their time budget (reproduced: FAILED)")
        for line in _REPRODUCED_OVERRUNS:
            terminalreporter.line(f"FAILED {line}")
    if _NOISE:
        terminalreporter.section("tests over their time budget (warning only)")
        for line in _NOISE:
            terminalreporter.line(line)
            # GitHub Actions annotation.
            terminalreporter.line(f"::warning::{line}")
