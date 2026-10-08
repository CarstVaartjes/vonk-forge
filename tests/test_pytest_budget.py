"""One over-budget test never fails a run: only a reproduced overrun does."""

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
    tmp_path: Path, body: str, calibration: str = "1"
) -> subprocess.CompletedProcess[str]:
    """Run ``body`` as the module of one test under the budget plugin."""

    (tmp_path / "test_sleep.py").write_text(body, encoding="utf-8")
    # Inject measured report durations, not wall-clock sleeps: loaded hosts
    # must not make the regression tests for runner noise flaky themselves.
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
            "-p",
            "no:cacheprovider",
            "-p",
            "tools.pytest_budget",
            "--test-budget-scale=0.01",
            "--rootdir",
            str(tmp_path),
            "-c",
            os.devnull,
            str(tmp_path / "test_sleep.py"),
        ],
        cwd=ROOT,
        env={
            # This subprocess owns its own initial measurement and isolated
            # retry. An outer budget retry must not pre-mark its first pass.
            **{
                key: value
                for key, value in os.environ.items()
                if key != pytest_budget._ISOLATED_ENV
            },
            "PYTHONPATH": str(ROOT),
            pytest_budget.CALIBRATION_ENV: calibration,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _durations(tmp_path: Path, first: float, later: float) -> str:
    """A test reporting ``first`` seconds initially and ``later`` on verification."""

    marker = tmp_path / "ran"
    return (
        "import pathlib\n\n"
        "def test_sleeps(request):\n"
        f"    marker = pathlib.Path({str(marker)!r})\n"
        "    seen = marker.exists()\n"
        "    marker.write_text(marker.read_text() + 'x' if seen else 'x')\n"
        f"    request.node.user_properties.append(('synthetic-duration', {later!r} if seen else {first!r}))\n"
    )


def test_a_one_off_overrun_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    """Catches failing the run on a single stalled measurement."""

    result = _run(tmp_path, _durations(tmp_path, first=0.3, later=0.0))
    assert result.returncode == 0, result.stdout
    assert "::warning::" in result.stdout and "test_sleeps" in result.stdout
    assert "Not reproduced when run alone" in result.stdout


def test_an_overrun_that_reproduces_alone_fails(tmp_path: Path) -> None:
    """Catches a budget that no longer fails a test that is genuinely slow."""

    result = _run(tmp_path, _durations(tmp_path, first=0.3, later=0.3))
    assert result.returncode == 1, result.stdout
    assert "Reproduced when run alone" in result.stdout
    assert "::warning::" not in result.stdout


@pytest.mark.parametrize("later, expected", [(0.0, 0), (100.0, 1)])
def test_even_a_far_overrun_requires_a_second_measurement(
    tmp_path: Path, later: float, expected: int
) -> None:
    """Catches the immediate >5x failure bypassing runner-noise verification."""

    result = _run(tmp_path, _durations(tmp_path, first=100.0, later=later))
    assert result.returncode == expected, result.stdout
    assert (tmp_path / "ran").read_text() == "xx"


def test_the_runner_calibration_scales_the_budget(tmp_path: Path) -> None:
    """Catches a budget that ignores how slow the runner measured."""

    body = "def test_sleeps(request):\n    request.node.user_properties.append(('synthetic-duration', 0.3))\n"
    assert _run(tmp_path, body, calibration="1").returncode == 1
    scaled = _run(tmp_path, body, calibration="4")
    assert scaled.returncode == 0, scaled.stdout
    assert "::warning::" not in scaled.stdout


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


def test_only_the_overrun_that_reproduces_fails_among_several(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches one rerun verdict being applied to every suspect in the session."""

    monkeypatch.setenv(pytest_budget._ISOLATED_ENV, "1")
    noisy, slow = tmp_path / "noisy", tmp_path / "slow"
    body = (
        "import pathlib\n\n"
        "def _measure(request, marker, first, later):\n"
        "    seen = pathlib.Path(marker).exists()\n"
        "    pathlib.Path(marker).write_text('x')\n"
        "    request.node.user_properties.append(('synthetic-duration', later if seen else first))\n\n"
        f"def test_noisy(request):\n    _measure(request, {str(noisy)!r}, 0.3, 0.0)\n\n"
        f"def test_slow(request):\n    _measure(request, {str(slow)!r}, 0.3, 0.3)\n"
    )
    result = _run(tmp_path, body)
    assert result.returncode == 1, result.stdout
    assert "FAILED test_sleep.py::test_slow" in result.stdout
    assert "::warning::test_sleep.py::test_noisy" in result.stdout
    assert "FAILED test_sleep.py::test_noisy" not in result.stdout


def test_the_rerun_keeps_the_session_calibration() -> None:
    """Catches a rerun that calibrates again on a quieter host and so judges the
    overrun against a tighter budget than the session used (batch 7a release:
    12.4 s alone against a 17.7 s session budget, reported as reproduced)."""

    from types import SimpleNamespace

    config = SimpleNamespace(stash=pytest.Stash())
    config.stash[pytest_budget._CALIBRATION] = 1.77
    env = pytest_budget._rerun_environment(config)  # type: ignore[arg-type]
    assert float(env[pytest_budget.CALIBRATION_ENV]) == pytest.approx(1.77)


def test_the_rerun_uses_the_calibration_the_worker_judged_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an xdist controller (calibrated alone, ~1x) rerunning a suspect a
    worker judged at ~1.8x against its own tighter budget (#1164 release)."""

    from types import SimpleNamespace

    monkeypatch.setattr(pytest_budget, "_SUSPECT_CALIBRATIONS", {})
    monkeypatch.setattr(pytest_budget, "_SUSPECTS", {})
    report = SimpleNamespace(
        user_properties=[
            (pytest_budget._SUSPECT_PROPERTY, "t.py::test_x\t1.78\ttest took 19.4s")
        ]
    )
    pytest_budget.pytest_runtest_logreport(report)  # type: ignore[arg-type]
    assert pytest_budget._SUSPECTS == {"t.py::test_x": "test took 19.4s"}
    config = SimpleNamespace(stash=pytest.Stash())
    config.stash[pytest_budget._CALIBRATION] = 1.0
    env = pytest_budget._rerun_environment(config)  # type: ignore[arg-type]
    assert float(env[pytest_budget.CALIBRATION_ENV]) == pytest.approx(1.78)


def test_inconclusive_budget_verification_does_not_fail_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout has no second duration; it cannot prove a sustained regression."""
    from types import SimpleNamespace

    monkeypatch.setattr(pytest_budget, "_SUSPECTS", {"t.py::test_x": "test took 100s"})
    monkeypatch.setattr(pytest_budget, "_SUSPECT_CALIBRATIONS", {})
    monkeypatch.setattr(pytest_budget, "_REPRODUCED_OVERRUNS", [])
    monkeypatch.setattr(pytest_budget, "_NOISE", [])

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("pytest", pytest_budget.RERUN_TIMEOUT_SECONDS)

    monkeypatch.setattr(pytest_budget.subprocess, "run", timeout)
    config = SimpleNamespace(
        stash=pytest.Stash(),
        getoption=lambda _name: 1.0,
        rootpath=ROOT,
        inipath=None,
        invocation_params=SimpleNamespace(args=()),
    )
    session = SimpleNamespace(config=config, exitstatus=pytest.ExitCode.OK)
    pytest_budget.pytest_sessionfinish(session)  # type: ignore[arg-type]
    assert session.exitstatus == pytest.ExitCode.OK
    assert not pytest_budget._REPRODUCED_OVERRUNS
    assert any("no second measurement" in line for line in pytest_budget._NOISE)
    assert any("Verification inconclusive" in line for line in pytest_budget._NOISE)
    assert not any("treated as runner noise" in line for line in pytest_budget._NOISE)
