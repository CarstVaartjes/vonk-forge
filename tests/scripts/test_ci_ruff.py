"""Execute the Ruff workflow shell so discovery failures cannot skip lint."""

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _run(tmp_path, *, diff_exit=0, lint_exit=0, changed="module.py\n", base="base"):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = next(
        s for s in workflow["jobs"]["lint"]["steps"] if s.get("name") == "Run Ruff"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "lint-calls"
    git = bin_dir / "git"
    git.write_text(
        "#!/bin/bash\n"
        '[[ "$*" == *"base head"* ]] || exit 92\n'
        f"printf '%b' {changed!r}\nexit {diff_exit}\n"
    )
    uv = bin_dir / "uv"
    uv.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{calls}"\nexit {lint_exit}\n')
    git.chmod(0o755)
    uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_SHA": "head",
        "RUNNER_TEMP": str(tmp_path),
    }
    env.pop("GITHUB_BASE_SHA", None)
    for key, value in step.get("env", {}).items():
        env[key] = (
            base if value == "${{ github.event.pull_request.base.sha }}" else value
        )
    done = subprocess.run(
        ["bash", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return done, calls.read_text() if calls.exists() else ""


def test_pr_base_is_supplied_and_changed_python_is_linted(tmp_path):
    done, calls = _run(tmp_path)
    assert done.returncode == 0, done.stderr
    assert "ruff check --force-exclude module.py" in calls


def test_failed_git_diff_is_not_reported_as_no_changed_python(tmp_path):
    done, calls = _run(tmp_path, diff_exit=23, changed="")
    assert done.returncode == 23
    assert not calls
    assert "skipping Ruff" not in done.stdout


def test_missing_pr_base_fails_before_lint(tmp_path):
    done, calls = _run(tmp_path, base="")
    assert done.returncode != 0
    assert not calls


def test_ruff_failure_is_preserved(tmp_path):
    done, calls = _run(tmp_path, lint_exit=17)
    assert done.returncode == 17
    assert calls


def test_empty_successful_diff_skips_lint(tmp_path):
    done, calls = _run(tmp_path, changed="")
    assert done.returncode == 0, done.stderr
    assert not calls
    assert "No changed Python files" in done.stdout
