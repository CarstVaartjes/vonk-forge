from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "collect-pytest-file-durations"


def _module():
    loader = importlib.machinery.SourceFileLoader(
        "collect_pytest_file_durations", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_collect_sums_cases_and_normalizes_absolute_paths(tmp_path: Path) -> None:
    module = _module()
    report = tmp_path / "junit.xml"
    test_file = tmp_path / "tests" / "slow.py"
    report.write_text(
        "<testsuite>"
        f'<testcase file="{test_file}" time="1.25"/>'
        f'<testcase file="{test_file}" time="2.75"/>'
        '<testcase file="/outside/tests/slow.py" time="99"/>'
        '<testcase file="tests/nan.py" time="nan"/>'
        '<testcase classname="missing" time="10"/>'
        "</testsuite>"
    )
    assert module.collect(report, root=tmp_path) == {"tests/slow.py": 4.0}


def test_collect_reads_pytest_xunit1_report(tmp_path: Path) -> None:
    module = _module()
    test_file = tmp_path / "test_tiny.py"
    test_file.write_text("def test_one(): pass\ndef test_two(): pass\n")
    report = tmp_path / "junit.xml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "junit_family=xunit1",
            f"--junitxml={report}",
            str(test_file),
        ],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    durations = module.collect(report, root=tmp_path)
    assert set(durations) == {"test_tiny.py"}
    assert durations["test_tiny.py"] >= 0
