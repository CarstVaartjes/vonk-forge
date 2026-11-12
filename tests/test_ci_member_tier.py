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
FULL = "needs.changes.outputs.full == 'true'"
# The reviewed jobs a member pull request runs. The lane proof reads `lane`,
# which the selector already folds `full` into; the gate aggregates everything.
MEMBER_JOBS = {
    "changes",
    "lint",
    "supply-chain",
    "repository-guards",
    "generated-clients",
    "targeted-tests",
    "rust-quality",
    "rust-tests",
    "lane-proof",
    "ci-gate",
}


def _conjuncts(condition: str) -> list[str]:
    """Split a GitHub expression at its top-level `&&`, ignoring parentheses."""
    text = condition.strip().removeprefix("${{").removesuffix("}}").strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        for index, char in enumerate(text):
            depth += {"(": 1, ")": -1}.get(char, 0)
            if depth == 0 and index < len(text) - 1:
                break
        else:
            text = text[1:-1].strip()
            continue
        break
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth == 0 and text.startswith("&&", index):
            parts.append(text[start:index].strip())
            start = index + 2
    return [*parts, text[start:].strip()]


def _gated(jobs: dict) -> set[str]:
    """Jobs that never run on a member pull request.

    A job is gated by a top-level `full` or push-only conjunct, by requiring a
    gated job's success, or, without its own `if`, by needing a gated job.
    """
    gated: set[str] = set()
    while True:
        before = len(gated)
        for name, job in jobs.items():
            needs = job.get("needs", [])
            needs = [needs] if isinstance(needs, str) else needs
            if "if" not in job:
                if any(need in gated for need in needs):
                    gated.add(name)
                continue
            for term in _conjuncts(str(job["if"])):
                need = term.removeprefix("needs.").split(".result == 'success'")[0]
                if term in (FULL, "github.event_name == 'push'") or (
                    term.endswith(".result == 'success'") and need in gated
                ):
                    gated.add(name)
        if len(gated) == before:
            return gated


def test_every_full_suite_job_is_gated_by_the_selector() -> None:
    jobs = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())["jobs"]
    assert set(jobs) - _gated(jobs) == MEMBER_JOBS
    for step in jobs["ci-gate"]["steps"]:
        # A member run's receipt would let Release skip CI on an identical tree.
        if "write-receipt" in step.get("run", "") or "ci-tested-source" in str(
            step.get("with", {}).get("name")
        ):
            assert FULL in _conjuncts(step["if"])


def test_conjuncts_are_parsed_not_matched() -> None:
    assert _conjuncts(f"({FULL} && a) || b") == [f"({FULL} && a) || b"]
    assert _conjuncts(f"{FULL} && (a || b)") == [FULL, "(a || b)"]
    assert _conjuncts(f"(a) && ({FULL})") == ["(a)", f"({FULL})"]


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
