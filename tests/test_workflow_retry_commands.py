"""A command wrapped in retry-dependency-fetch must be one the script accepts.

The script refuses (exit 64) anything outside its acquisition allowlist, so a
workflow that wraps another command fails at run time, after merge. The
allowlist is read from the script itself.
"""

import os
import shlex
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from retry_dependency_fetch import acquisition_prefix

WRAPPER = "scripts/retry-dependency-fetch"
# The hash-locked pip form is checked against RUNNER_TEMP by the script at run time.
PIP_FORM = ("uv", "pip", "install", "--python")


def wrapped_commands(script: str) -> list[tuple[str, ...]]:
    found = []
    for line in script.replace("\\\n", " ").splitlines():
        if WRAPPER not in line:
            continue
        words = shlex.split(line.split(WRAPPER, 1)[1], comments=True)
        if words:
            found.append((os.path.basename(words[0]), *words[1:]))
    return found


def refused(script: str) -> list[tuple[str, ...]]:
    return [
        words
        for words in wrapped_commands(script)
        if not (acquisition_prefix(words) or words[:4] == PIP_FORM)
    ]


def run_scripts(document: dict) -> list[str]:
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
