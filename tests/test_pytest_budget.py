"""Timing overruns are reported without failing or rerunning tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools import pytest_budget
from tools.pytest_budget import (
    MAX_CALIBRATION,
    calibration_factor,
)

ROOT = Path(__file__).resolve().parents[1]


def _run(
    tmp_path: Path, body: str, calibration: str = "1", *extra: str
) -> subprocess.CompletedProcess[str]:
    """Run ``body`` as the module of one test under the budget plugin."""

    (tmp_path / "test_sleep.py").write_text(body, encoding="utf-8")
    # Inject measured report durations, not wall-clock sleeps: loaded hosts
    # must not make the timing-report regression tests flaky themselves.
    (tmp_path / "conftest.py").write_text(
        "import pytest\n\n"
        "@pytest.hookimpl(wrapper=True, trylast=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    report = yield\n"
        "    report.duration = 0.0\n"
        "    if report.when == 'call':\n"
        "        for name, value in report.user_properties:\n"
        "            if name == 'synthetic-duration':\n"
        "                report.duration = float(value)\n"
        "    return report\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--junitxml",
            str(tmp_path / "results.xml"),
            "-p",
            "no:cacheprovider",
            "-p",
            "tools.pytest_budget",
            "--test-budget-scale=0.01",
            "--rootdir",
            str(tmp_path),
            "-c",
            os.devnull,
            *extra,
            str(tmp_path / "test_sleep.py"),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            pytest_budget.CALIBRATION_ENV: calibration,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("duration", [0.3, 100.0])
def test_an_overrun_is_reported_without_failure_or_rerun(
    tmp_path: Path, duration: float
) -> None:
    """Catches failing even a large overrun or executing the test a second time."""

    marker = tmp_path / "ran"
    body = (
        "import pathlib\n\n"
        "def test_sleeps(request):\n"
        f"    marker = pathlib.Path({str(marker)!r})\n"
        "    marker.write_text(marker.read_text() + 'x' if marker.exists() else 'x')\n"
        f"    request.node.user_properties.append(('synthetic-duration', {duration!r}))\n"
    )
    result = _run(tmp_path, body)
    assert result.returncode == 0, result.stdout
    assert "slow tests" in result.stdout
    assert "test_sleep.py::test_sleeps: test took" in result.stdout
    assert "over its 0.1s budget" in result.stdout
    assert marker.read_text() == "x"
    assert "vonk-test-budget-overrun" in (tmp_path / "results.xml").read_text()


def test_the_runner_calibration_scales_reporting(tmp_path: Path) -> None:
    """Catches reporting an overrun against an unscaled threshold."""

    body = "def test_sleeps(request):\n    request.node.user_properties.append(('synthetic-duration', 0.3))\n"
    unscaled = _run(tmp_path, body, calibration="1")
    assert unscaled.returncode == 0, unscaled.stdout
    assert "slow tests" in unscaled.stdout
    scaled = _run(tmp_path, body, calibration="4")
    assert scaled.returncode == 0, scaled.stdout
    assert "slow tests" not in scaled.stdout


def test_slow_marker_raises_the_reporting_threshold(tmp_path: Path) -> None:
    """Catches dropping slow marker registration or ignoring its threshold."""

    body = (
        "import pytest\n\n"
        "@pytest.mark.slow(40)\n"
        "def test_sleeps(request):\n"
        "    request.node.user_properties.append(('synthetic-duration', 0.3))\n"
    )
    result = _run(tmp_path, body)
    assert result.returncode == 0, result.stdout
    assert "slow tests" not in result.stdout
    assert "UnknownMark" not in result.stdout


def test_calibration_factor_follows_the_measured_speed() -> None:
    reference = 0.01
    assert calibration_factor(0.01, reference) == 1.0
    assert calibration_factor(0.03, reference) == pytest.approx(3.0)
    # A faster machine never tightens a budget; a hopeless one is capped.
    assert calibration_factor(0.001, reference) == 1.0
    assert calibration_factor(10.0, reference) == MAX_CALIBRATION


def test_the_benchmark_uses_median_wall_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CPU time or the fastest sample hides contention on a loaded runner."""
    measured = iter([0.0, 0.1, 1.0, 1.8, 2.0, 2.5])
    workloads: list[int] = []
    monkeypatch.setattr(pytest_budget.time, "perf_counter", lambda: next(measured))
    monkeypatch.setattr(
        pytest_budget, "_benchmark_workload", lambda: workloads.append(1)
    )
    assert pytest_budget.benchmark_seconds() == pytest.approx(0.5)
    assert len(workloads) == 3


def test_a_listed_test_fails_its_overrun_while_an_unlisted_one_only_reports(
    tmp_path: Path,
) -> None:
    """Catches enforcement that fails every overrun, none, or reruns the test;
    the listing is matched on the repository-relative path and the function
    name, never on the parameter id."""

    body = (
        "import pytest\n\n"
        "@pytest.mark.parametrize('value', [1])\n"
        "def test_changed(request, value):\n"
        "    request.node.user_properties.append(('synthetic-duration', 100.0))\n\n"
        "def test_untouched(request):\n"
        "    request.node.user_properties.append(('synthetic-duration', 100.0))\n"
    )
    listing = tmp_path / "changed-tests.txt"
    identity = f"{(tmp_path / 'test_sleep.py').resolve().as_posix()}::test_changed"
    listing.write_text(identity + "\n")
    result = _run(tmp_path, body, "1", f"--test-budget-enforce={listing}")
    assert result.returncode == 1, result.stdout
    assert "1 failed, 1 passed" in result.stdout
    assert "test_changed[1]: test took" in result.stdout
    assert "added or changed by this change" in result.stdout
    assert "slow tests" in result.stdout
