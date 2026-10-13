from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "collect-pytest-file-durations"
)


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


@pytest.mark.parametrize("family", [None, "xunit1"])
def test_collect_reads_actual_pytest_reports(
    tmp_path: Path, family: str | None
) -> None:
    """Default JUnit output must not silently drop normal tests from shard weights."""
    module = _module()
    directory = tmp_path / "tests" / "nested"
    directory.mkdir(parents=True)
    test_file = directory / "test_tiny.py"
    test_file.write_text(
        "def test_one(): pass\nclass TestGroup:\n    def test_two(self): pass\n"
    )
    (tmp_path / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.hookimpl(wrapper=True, trylast=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    report = yield\n"
        "    report.duration = (2.75 if item.cls else 1.25) if report.when == 'call' else 0\n"
        "    return report\n"
    )
    report = tmp_path / "junit.xml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *(["-o", f"junit_family={family}"] if family else []),
            f"--junitxml={report}",
            str(test_file),
        ],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    durations = module.collect(report, root=tmp_path)
    assert set(durations) == {"tests/nested/test_tiny.py"}
    assert durations["tests/nested/test_tiny.py"] == 4.0
