"""A command wrapped in retry-dependency-fetch must be one the script accepts.

The script refuses (exit 64) anything outside its acquisition allowlist, so a
workflow that wraps another command fails at run time, after merge. The
allowlist is read from the script itself.
"""

import re
import shlex
from pathlib import Path
from typing import Any

import yaml
from retry_dependency_fetch import acquisition_prefix, locked_pip_shape

ROOT = Path(__file__).resolve().parents[1]

WRAPPER = "scripts/ci/retry-dependency-fetch"


def wrapped_commands(script: str) -> list[tuple[str, ...]]:
    found = []
    for line in script.replace("\\\n", " ").splitlines():
        for call in line.split(WRAPPER)[1:]:
            # Cut at a shell separator so a later command is not part of this one.
            words = shlex.split(re.split(r"&&|\|\||;|\|", call)[0], comments=True)
            if words:
                found.append((Path(words[0]).name, *words[1:]))
    return found


def refused(script: str) -> list[tuple[str, ...]]:
    return [
        words
        for words in wrapped_commands(script)
        if not (acquisition_prefix(words) or locked_pip_shape(words))
    ]


def run_scripts(document: dict[str, Any]) -> list[str]:
    steps = [*document.get("runs", {}).get("steps", [])]
    for job in document.get("jobs", {}).values():
        steps += job.get("steps", [])
    return [step["run"] for step in steps if "run" in step]


def test_wrapped_commands_are_on_the_script_allowlist() -> None:
    paths = [*(ROOT / ".github/workflows").glob("*.yml")]
    paths += (ROOT / ".github/actions").glob("**/action.yml")
    problems = [
        f"{path.relative_to(ROOT)}: {' '.join(words)}"
        for path in sorted(paths)
        for script in run_scripts(yaml.safe_load(path.read_text()))
        for words in refused(script)
    ]
    assert problems == []


def test_a_wrapped_install_of_a_cargo_tool_is_refused() -> None:
    script = f'{WRAPPER} cargo "+$N" install cargo-fuzz --locked\n{WRAPPER} cargo fetch \\\n  --locked'
    assert refused(script) == [("cargo", "+$N", "install", "cargo-fuzz", "--locked")]


def test_every_wrapper_call_on_a_line_is_checked() -> None:
    script = f"{WRAPPER} uv sync && {WRAPPER} cargo install x"
    assert refused(script) == [("cargo", "install", "x")]
    unhashed = f'{WRAPPER} uv pip install --python "$p" -r req.txt'
    assert refused(unhashed) == [
        ("uv", "pip", "install", "--python", "$p", "-r", "req.txt")
    ]


def test_every_rustup_download_goes_through_the_wrapper() -> None:
    bare = re.compile(r"(?<!fetch )rustup (toolchain install|target add|component add)")
    paths = [*(ROOT / ".github").rglob("*.yml")]
    problems = [
        f"{path.relative_to(ROOT)}: {line.strip()}"
        for path in sorted(paths)
        for line in path.read_text().splitlines()
        if bare.search(line) and not line.lstrip().startswith("#")
    ]
    assert problems == []
