"""Coverage is an on-demand report: it never runs for a pull request or a batch."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
COVERAGE = yaml.safe_load((WORKFLOWS / "coverage.yml").read_text())


def test_the_coverage_workflow_runs_only_when_dispatched() -> None:
    assert set(COVERAGE[True]) == {"workflow_dispatch"}


def test_no_workflow_calls_the_coverage_workflow() -> None:
    callers = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in yaml.safe_load(path.read_text())["jobs"].items():
            if "coverage.yml" in str(job.get("uses", "")):
                callers.append(f"{path.name}:{name}")
    assert callers == []


def test_the_report_lists_only_code_no_test_executes(tmp_path: Path) -> None:
    python = {
        "files": {
            "src/dead.py": {
                "executed_lines": [],
                "missing_lines": [1, 2],
                "functions": {},
            },
            "src/live.py": {
                "executed_lines": [1],
                "missing_lines": [4],
                "functions": {
                    "": {"executed_lines": [1], "missing_lines": []},
                    "used": {"executed_lines": [2], "missing_lines": []},
                    "unused": {"executed_lines": [], "missing_lines": [4]},
                },
            },
            "src/full.py": {
                "executed_lines": [1],
                "missing_lines": [],
                "functions": {},
            },
        }
    }
    report = tmp_path / "python.json"
    report.write_text(json.dumps(python))
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/dev/coverage-unexecuted"),
            "--python",
            str(report),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "`src/dead.py`" in result
    assert "src/live.py:4 `unused`" in result
    assert "used`" not in result.replace("unused`", "")
    assert "full.py" not in result
    assert "%" not in result
