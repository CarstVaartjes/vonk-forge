"""Catch unclassified scans, selector-dependent guards and a forgiving CI gate."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.repository_guards import GUARDS, SCOPED

ROOT = Path(__file__).resolve().parents[1]


def unclassified(root: Path) -> set[str]:
    covered = {guard.split("::", 1)[0] for guard in GUARDS}
    covered.update(file for file, _ in SCOPED)
    return {
        path.relative_to(root).as_posix()
        for path in root.joinpath("tests").rglob("test_*.py")
    } - covered


def test_every_test_file_has_a_reviewed_coverage_boundary() -> None:
    assert not unclassified(ROOT)
    scoped = [file for file, reason in SCOPED if reason.strip()]
    assert len(scoped) == len(SCOPED)
    assert len(set(scoped)) == len(scoped)
    assert len(set(GUARDS)) == len(GUARDS)
    assert not set(scoped) & {guard for guard in GUARDS if "::" not in guard}
    assert all(ROOT.joinpath(file).is_file() for file in scoped)
    assert all(ROOT.joinpath(guard.split("::", 1)[0]).is_file() for guard in GUARDS)


def test_new_scan_cannot_hide_behind_a_helper_or_a_scoped_directory(
    tmp_path: Path,
) -> None:
    # No syntax heuristic can prove the scope of a delegated scan. A new file
    # needs a decision even if the recursive traversal lives in an imported tool.
    file = tmp_path / "tests/scripts/test_new_scan.py"
    file.parent.mkdir(parents=True)
    file.write_text("from tools import scanner\ndef test_scan(): scanner.check()\n")
    assert unclassified(tmp_path) == {"tests/scripts/test_new_scan.py"}


def test_guard_runner_and_heavy_suite_use_complementary_selections() -> None:
    def selection(*args: str) -> list[str]:
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts/repository-guards"), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.splitlines()

    assert selection() == list(GUARDS)
    assert selection("--exclusions") == [
        f"{'--deselect' if '::' in guard else '--ignore'}={guard}" for guard in GUARDS
    ]


def test_ci_guards_are_unconditional_prepared_and_required() -> None:
    jobs = yaml.safe_load(ROOT.joinpath(".github/workflows/ci.yml").read_text())["jobs"]
    guards = jobs["repository-guards"]
    assert "if" not in guards
    assert "needs" not in guards
    steps = guards["steps"]
    prepared = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses") == "./.github/actions/python-env"
    )
    run_index = next(
        i
        for i, step in enumerate(steps)
        if "scripts/repository-guards" in step.get("run", "")
    )
    assert prepared < run_index
    assert "if" not in steps[run_index]
    assert (
        "uv run --project control --frozen python -m pytest" in steps[run_index]["run"]
    )
    assert any(
        "shellcheck" in step.get("with", {}).get("packages", "").split()
        for step in steps
    )
    gate = jobs["ci-gate"]
    assert "repository-guards" in gate["needs"]
    [verify] = [
        step
        for step in gate["steps"]
        if "scripts/verify-ci-gate" in step.get("run", "")
    ]
    assert (
        verify["env"]["REPOSITORY_GUARDS_RESULT"]
        == "${{ needs.repository-guards.result }}"
    )
    assert '--result "repository-guards=$REPOSITORY_GUARDS_RESULT"' in verify["run"]


@pytest.mark.parametrize(
    "event", ["pull_request", "merge_group", "push", "workflow_call"]
)
def test_control_only_change_keeps_guards_without_selecting_heavy_repository(
    event: str,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/select-ci-areas"),
            "--event",
            event,
            "--github-output",
        ],
        input="control/src/vonk_control/models.py\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    outputs = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert outputs["guards"] == "true"
    if event in {"pull_request", "merge_group"}:
        assert outputs["repository"] == "false"


@pytest.mark.parametrize("suite", ["repository", "guards"])
def test_real_suite_runner_applies_reviewed_selection(
    tmp_path: Path, suite: str
) -> None:
    # Replace only environment preparation and pytest, so the actual Bash
    # argument wiring is tested without collecting an expensive product suite.
    for file in (
        "scripts/test",
        "scripts/repository-guards",
        "tests/repository_guards.py",
    ):
        destination = tmp_path / file
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / file, destination)
    (tmp_path / "tests/__init__.py").write_text("")
    prepare = tmp_path / "scripts/sync-cli-dependencies"
    prepare.write_text("#!/bin/sh\nexit 0\n")
    prepare.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    uv.chmod(0o755)
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/test"), suite, "--", "--collect-only"],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    arguments = result.stdout.splitlines()
    if suite == "guards":
        assert arguments[-len(GUARDS) :] == list(GUARDS)
    else:
        assert "tests" == arguments[-1]
        assert all(
            f"{'--deselect' if '::' in guard else '--ignore'}={guard}" in arguments
            for guard in GUARDS
        )
