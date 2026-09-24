"""Exercise acceptance exit status through actual, isolated pytest processes."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


def run_acceptance(
    tmp_path: Path, source: str, *, required: bool = True, collect_only: bool = False
) -> subprocess.CompletedProcess[str]:
    test_file = tmp_path / "test_acceptance.py"
    test_file.write_text(source, encoding="utf-8")
    command = [sys.executable, "-m", "pytest", "-q", "-c", "/dev/null"]
    if required:
        command.extend(["-p", "control.tests.required_execution"])
    if collect_only:
        command.append("--collect-only")
    command.append(str(test_file))
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment.pop("PYTEST_ADDOPTS", None)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    "source",
    [
        "import pytest\ndef test_missing_engine(): pytest.skip('Docker unavailable')\n",
        "import pytest\npytest.importorskip('missing_acceptance_dependency')\n",
        "import pytest\n@pytest.mark.xfail\ndef test_fault(): assert False\n",
        "import pytest\n@pytest.mark.xfail\ndef test_unexpected_pass(): pass\n",
        "import pytest\ndef test_pass(): pass\ndef test_skip(): pytest.skip('missing asset')\n",
    ],
)
def test_unexecuted_acceptance_cannot_look_green(tmp_path: Path, source: str) -> None:
    ordinary = run_acceptance(tmp_path, source, required=False)
    # A collection skip may already produce NO_TESTS_COLLECTED. Mixed skips and
    # xfails otherwise demonstrate pytest's ordinary successful exit behavior.
    assert ordinary.returncode in (0, 5), ordinary.stdout + ordinary.stderr
    required = run_acceptance(tmp_path, source)
    assert required.returncode in (1, 5), required.stdout + required.stderr
    assert "Acceptance evidence incomplete" in required.stdout


def test_collection_is_not_execution_evidence(tmp_path: Path) -> None:
    result = run_acceptance(tmp_path, "def test_ready(): pass\n", collect_only=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "No selected test completed successfully" in result.stdout


@pytest.mark.parametrize("passes", [True, False])
def test_executed_outcome_is_preserved(tmp_path: Path, passes: bool) -> None:
    result = run_acceptance(tmp_path, f"def test_boundary(): assert {passes!r}\n")
    assert result.returncode == (0 if passes else 1), result.stdout + result.stderr
