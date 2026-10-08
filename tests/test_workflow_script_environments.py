"""A workflow runs a repository Python script inside the environment its imports need.

Batch 7b's release failed: ``scripts/build-nas-compose-bundle`` started importing the
shared Pydantic contracts, but the publication workflow ran it with the runner's bare
Python, which has no pydantic. Every direct workflow invocation of a repository Python
script whose module-level imports need a project package must go through ``uv run``
(a function-level import serves only the subcommands that reach it).
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
# A command line that starts by executing a repository script directly.
_DIRECT = re.compile(r"^\s*(?:[A-Z_]+=\S+\s+)*((?:scripts|tools)/[\w.-]+)(?:\s|$)")
_NEEDS_PROJECT = re.compile(
    r"^(?:from|import)\s+(pydantic|vonk_agent_protocol|vonk_control|vonk_cluster_profiles)\b",
    re.MULTILINE,
)


def _python_script_needing_project(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    shebang = text.splitlines()[0] if text.startswith("#!") else ""
    if "python" not in shebang or "uv run" in shebang:
        return None  # not Python, or it brings its own project environment
    found = _NEEDS_PROJECT.search(text)
    return found.group(1) if found else None


def test_workflows_run_project_python_scripts_through_uv() -> None:
    problems = []
    for workflow in WORKFLOWS:
        for number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), 1
        ):
            match = _DIRECT.match(line)
            if not match:
                continue
            module = _python_script_needing_project(ROOT / match.group(1))
            if module:
                problems.append(
                    f"{workflow.name}:{number}: {match.group(1)} imports {module}; "
                    "run it with `uv run --project control --frozen python ...`"
                )
    assert problems == []


def test_contract_generators_prepare_before_project_imports() -> None:
    """A bare interpreter must enter preparation before loading dependencies."""

    scripts = sorted(ROOT.joinpath("scripts").glob("generate-*")) + sorted(
        ROOT.joinpath("scripts").glob("export-*schema*")
    )
    checked = []
    for script in scripts:
        source = script.read_text()
        if "python" not in source.splitlines()[0]:
            continue
        tree = ast.parse(source)
        needs_project = (
            any(
                isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[0]
                in {
                    "pydantic",
                    "vonk_agent_protocol",
                    "vonk_control",
                    "vonk_forge_contracts",
                }
                for node in ast.walk(tree)
            )
            or "scripts/export-" in source
        )
        if not needs_project:
            continue
        # No site packages and a preparation sentinel: catches imports before
        # preparation as well as generators that never prepare at all.
        probe = """
import runpy, subprocess, sys, types
def unprepared(*args, **kwargs):
    raise AssertionError("generator effects started before preparation")
subprocess.run = unprepared
script = sys.argv[1]
sys.argv = [script]
helper = types.ModuleType("check_environment")
def prepare(root):
    raise SystemExit(73)
helper.run_in_control = prepare
sys.modules["check_environment"] = helper
runpy.run_path(script, run_name="__main__")
"""
        result = subprocess.run(
            [sys.executable, "-S", "-c", probe, str(script)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 73, f"{script.name}: {result.stderr}"
        checked.append(script.name)
    assert "generate-agent-wire" in checked
