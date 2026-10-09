"""Execute the Ruff workflow shell to catch diff-driven lint omissions."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _run(tmp_path: Path, *, lint_exit: int = 0, event: str = "pull_request"):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        s for s in workflow["jobs"]["lint"]["steps"] if s.get("name") == "Run Ruff"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    calls = tmp_path / "lint-calls"
    uv = bin_dir / "uv"
    uv.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{calls}"\nexit {lint_exit}\n')
    uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_EVENT_NAME": event,
        "RUNNER_TEMP": str(tmp_path),
    }
    env.pop("GITHUB_BASE_SHA", None)
    env.pop("GITHUB_SHA", None)
    done = subprocess.run(
        ["bash", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return done, calls.read_text() if calls.exists() else ""


@pytest.mark.parametrize(
    "event", ["pull_request", "merge_group", "push", "workflow_dispatch"]
)
def test_whole_tree_lint_needs_no_diff_or_base(tmp_path: Path, event: str) -> None:
    done, calls = _run(tmp_path, event=event)
    assert done.returncode == 0, done.stderr
    assert "ruff check ." in calls
    assert "ruff format --check ." in calls


def test_ruff_failure_ends_only_that_run_and_a_fresh_run_is_admitted(
    tmp_path: Path,
) -> None:
    done, calls = _run(tmp_path / "failed", lint_exit=17)
    assert done.returncode == 17
    assert "ruff check ." in calls
    assert "ruff format" not in calls
    fresh, calls = _run(tmp_path / "fresh")
    assert fresh.returncode == 0, fresh.stderr
    assert "ruff format --check ." in calls
