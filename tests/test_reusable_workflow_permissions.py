"""A called workflow job may not need more than its caller grants.

GitHub refuses to start a run (startup_failure) when a job in a reusable
workflow asks for a permission its caller does not grant, so every release
on main stops before any job runs.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
RANK = {"none": 0, "read": 1, "write": 2}


def _excess(path: Path, granted: dict | None, chain: str) -> list[str]:
    workflow = yaml.safe_load(path.read_text()) or {}
    default = workflow.get("permissions")
    found: list[str] = []
    for name, job in (workflow.get("jobs") or {}).items():
        uses = job.get("uses", "")
        wanted = job.get("permissions", default)
        if granted is not None and not uses and isinstance(wanted, dict):
            for scope, level in wanted.items():
                if RANK[level] > RANK[granted.get(scope, "none")]:
                    found.append(
                        f"{chain}::{name} needs {scope}: {level}, "
                        f"caller grants {granted.get(scope, 'none')}"
                    )
        if uses.startswith("./.github/workflows/"):
            called = job.get("permissions")
            grant = called if isinstance(called, dict) else default
            found += _excess(
                ROOT / uses.removeprefix("./"),
                grant if isinstance(grant, dict) else granted,
                f"{chain} > {uses.rsplit('/', 1)[-1]}",
            )
    return found


def test_called_jobs_need_no_more_than_their_caller_grants() -> None:
    excess = [
        problem
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for problem in _excess(path, None, path.name)
    ]
    assert excess == []
