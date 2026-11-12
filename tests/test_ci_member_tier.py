"""One selector output decides whether a run carries the full suites.

A member pull request runs the fast tier; its integration pull request runs
every suite and proof once. `full` from scripts/select-ci-areas owns that rule,
so a new heavy job or proof workflow cannot run on every member again, and no
workflow re-derives the rule from the branch name.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
FULL = "needs.changes.outputs.full"
# Jobs that read the selector but run on member pull requests too: generated
# client determinism is fast and decisive, and the gate aggregates every job.
# The lane reads `lane`, which the selector job already folds `full` into.
UNGATED_CONSUMERS = {"generated-clients", "ci-gate", "lane-proof"}
# Jobs without needs run on every pull request, members included.
MEMBER_ROOTS = {"changes", "lint", "supply-chain", "repository-guards"}


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


def test_every_full_suite_job_is_gated_by_the_selector() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    roots = {name for name, job in jobs.items() if "needs" not in job}
    assert roots == MEMBER_ROOTS
    ungated = [
        name
        for name, job in jobs.items()
        if "changes" in job.get("needs", ())
        and name not in UNGATED_CONSUMERS
        and FULL not in job.get("if", "")
    ]
    assert ungated == []
    assert "steps.areas.outputs.full == 'true' &&" in jobs["changes"]["outputs"]["lane"]
    receipts = [
        step
        for step in jobs["ci-gate"]["steps"]
        if "write-receipt" in step.get("run", "")
        or "ci-tested-source" in str(step.get("with", {}).get("name"))
    ]
    assert len(receipts) == 2
    assert all(FULL in step["if"] for step in receipts)


def test_pull_request_proof_workflows_read_the_same_selector() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        assert "github.head_ref" not in path.read_text(), path.name
        if path.name == "ci.yml" or "pull_request" not in workflow[True]:
            continue
        jobs = workflow["jobs"]
        assert jobs["scope"]["uses"] == "./.github/workflows/ci-scope.yml"
        for name, job in jobs.items():
            if name != "scope":
                assert job["needs"] == "scope", (path.name, name)
                assert job["if"] == "needs.scope.outputs.full == 'true'"
