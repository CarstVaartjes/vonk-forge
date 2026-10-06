"""One over-budget test never fails a run: only a reproduced or far overrun does."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools import pytest_budget
from tools.pytest_budget import (
    FAR_BEYOND_BUDGET,
    MAX_CALIBRATION,
    calibration_factor,
)

ROOT = Path(__file__).resolve().parents[1]

# A 0.1 s budget (scale 0.01 of the 10 s default, calibration pinned to 1).
_BUDGET = 0.1


def _run(
    tmp_path: Path, body: str, calibration: str = "1"
) -> subprocess.CompletedProcess[str]:
    """Run ``body`` as the module of one test under the budget plugin."""

    (tmp_path / "test_sleep.py").write_text(body, encoding="utf-8")
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
            **os.environ,
            "PYTHONPATH": str(ROOT),
            pytest_budget.CALIBRATION_ENV: calibration,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _sleeps(tmp_path: Path, first: float, later: float) -> str:
    """A test that sleeps ``first`` seconds on its first run and ``later`` after."""

    marker = tmp_path / "ran"
    return (
        "import pathlib, time\n\n"
        "def test_sleeps():\n"
        f"    marker = pathlib.Path({str(marker)!r})\n"
        "    seen = marker.exists()\n"
        "    marker.write_text('x')\n"
        f"    time.sleep({later!r} if seen else {first!r})\n"
    )


def test_a_one_off_overrun_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    """Catches failing the run on a single stalled measurement."""

    result = _run(tmp_path, _sleeps(tmp_path, first=0.3, later=0.0))
    assert result.returncode == 0, result.stdout
    assert "::warning::" in result.stdout and "test_sleeps" in result.stdout
    assert "Not reproduced when run alone" in result.stdout


def test_an_overrun_that_reproduces_alone_fails(tmp_path: Path) -> None:
    """Catches a budget that no longer fails a test that is genuinely slow."""

    result = _run(tmp_path, _sleeps(tmp_path, first=0.3, later=0.3))
    assert result.returncode == 1, result.stdout
    assert "Reproduced when run alone" in result.stdout
    assert "::warning::" not in result.stdout


def test_a_far_overrun_fails_without_a_rerun(tmp_path: Path) -> None:
    """Catches rerunning (and so excusing) a test far beyond any runner noise."""

    far = _BUDGET * FAR_BEYOND_BUDGET * 1.5
    result = _run(tmp_path, _sleeps(tmp_path, first=far, later=0.0))
    assert result.returncode == 1, result.stdout
    assert "no runner noise explains that" in result.stdout


def test_the_runner_calibration_scales_the_budget(tmp_path: Path) -> None:
    """Catches a budget that ignores how slow the runner measured."""

    body = "import time\n\ndef test_sleeps():\n    time.sleep(0.3)\n"
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


def test_the_benchmark_measures_wall_time_of_this_machine() -> None:
    assert 0 < pytest_budget.benchmark_seconds(rounds=2) < 5.0


def test_only_the_overrun_that_reproduces_fails_among_several(tmp_path: Path) -> None:
    """Catches one rerun verdict being applied to every suspect in the session."""

    noisy, slow = tmp_path / "noisy", tmp_path / "slow"
    body = (
        "import pathlib, time\n\n"
        "def _sleep(marker, first, later):\n"
        "    seen = pathlib.Path(marker).exists()\n"
        "    pathlib.Path(marker).write_text('x')\n"
        "    time.sleep(later if seen else first)\n\n"
        f"def test_noisy():\n    _sleep({str(noisy)!r}, 0.3, 0.0)\n\n"
        f"def test_slow():\n    _sleep({str(slow)!r}, 0.3, 0.3)\n"
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
