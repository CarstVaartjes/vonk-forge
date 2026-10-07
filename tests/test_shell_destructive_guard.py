"""A recursive remove never takes a path from a variable that may be empty.

An empty variable turned a cleanup of `~/$directory` into `rm -rf ~/`, which
deleted a shared VM's home.  Every recursive remove in a shell script of this
repository therefore spells its variable operands `"${name:?}"`, which stops the
script when the variable is empty or unset, and `shellcheck` runs with the
quoting rules (SC2115, SC2086) as errors.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL_DIRECTORIES = ("scripts", "tools", "install", "packaging", "deploy", ".githooks")
_SHEBANG = re.compile(rb"^#!.*\b(?:ba|da|z)?sh\b")
# A recursive remove (``rm -rf``, ``rm -fr``, ``rm -r``), also through a path.
_RECURSIVE_REMOVE = re.compile(
    r"(?:^|[\s;&|(`'\"])(?:/\S*/)?rm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*\s+)"
)
_VARIABLE = re.compile(r"\$(?:\{(\w+)([^}]*)\}|(\w+))")
_HOME_VARIABLE_PATH = re.compile(r"(?:~|\$HOME|\$\{HOME\})/\$")


def shell_scripts(root: Path = ROOT) -> list[Path]:
    found = []
    for directory in SHELL_DIRECTORIES:
        for path in sorted((root / directory).rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as stream:
                first = stream.readline()
            if _SHEBANG.match(first) or path.suffix == ".sh":
                found.append(path)
    return found


def unguarded_removes(text: str) -> list[str]:
    """Lines of a script whose recursive remove names an unguarded variable."""

    problems = []
    for number, line in enumerate(text.split("\n"), start=1):
        if line.lstrip().startswith("#") or not _RECURSIVE_REMOVE.search(line):
            continue
        tail = line[_RECURSIVE_REMOVE.search(line).end() - 1 :]  # type: ignore[union-attr]
        if _HOME_VARIABLE_PATH.search(tail):
            problems.append(f"{number}: a path under home built from a variable")
            continue
        for match in _VARIABLE.finditer(tail):
            name, modifier = match.group(1), match.group(2) or ""
            if name is None or not modifier.startswith(":?"):
                problems.append(f"{number}: variable not guarded with ${{name:?}}")
                break
    return problems


def test_every_recursive_remove_guards_its_variables() -> None:
    failures = {
        path.relative_to(ROOT).as_posix(): problems
        for path in shell_scripts()
        if (problems := unguarded_removes(path.read_text(encoding="utf-8")))
    }
    assert failures == {}


@pytest.mark.parametrize(
    "line",
    [
        "rm -rf ~/$directory",
        'rm -rf "$HOME/$directory"',
        'rm -rf "$work"',
        "trap 'rm -rf -- \"$scratch\"' EXIT",
        'rm -fr "${work}"',
        '/usr/bin/rm -rf -- "$verify_root"',
    ],
)
def test_the_scan_rejects_the_unguarded_forms(line: str) -> None:
    assert unguarded_removes(line) != []


@pytest.mark.parametrize(
    "line",
    [
        'rm -rf -- "${work:?}"',
        "trap 'rm -rf -- \"${scratch:?}\"' EXIT",
        "rm -rf /var/lib/apt/lists/*",
        'docker run --rm --network none "$image"',
        'rm -f "$RUNNER_TEMP/key.pem"',
        '# rm -rf "$unguarded"',
    ],
)
def test_the_scan_accepts_guarded_and_unrelated_commands(line: str) -> None:
    assert unguarded_removes(line) == []


# shellcheck parses every repository shell script, which takes a while.
@pytest.mark.slow(60)
def test_shellcheck_quoting_rules_pass() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        if os.environ.get("CI") == "true":
            pytest.fail("shellcheck is required in CI")
        pytest.skip("shellcheck is not installed")
    result = subprocess.run(
        [shellcheck, "-i", "SC2115,SC2086", "-f", "gcc", "-S", "style"]
        + [str(path) for path in shell_scripts()],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_shell_discovery_includes_files_without_a_git_index(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "new-script"
    script.write_text('#!/bin/sh\nrm -rf "$work"\n')
    (scripts / "notes.txt").write_text("ordinary documentation\n")
    assert shell_scripts(tmp_path) == [script]
    assert unguarded_removes(script.read_text())
