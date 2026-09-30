"""The release workflow is one fan-in over CI and the development producers."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
RELEASE = WORKFLOWS / "installer-publication.yml"
PRODUCERS = {
    "ci": "ci.yml",
    "agent": "agent-release.yml",
    "images": "dev-images.yml",
    "setups": "installer-setups.yml",
}
R2_WRITERS = ("apt", "candidate", "promote")
LEVEL = {"none": 0, "read": 1, "write": 2}


def load(path: Path) -> dict:
    document = yaml.safe_load(path.read_text())
    # PyYAML reads the bare `on:` key as the boolean True.
    document["on"] = document.pop(True, document.get("on"))
    return document


def test_pushes_to_main_and_release_tags_are_the_only_release_triggers() -> None:
    release = load(RELEASE)
    assert release["on"] == {
        "push": {"branches": ["main"], "tags": ["v*"]},
        "workflow_dispatch": None,
    }
    for path in WORKFLOWS.glob("*.yml"):
        assert "workflow_run" not in load(path)["on"], path.name


def test_producers_run_only_when_the_release_calls_them() -> None:
    ci = load(WORKFLOWS / "ci.yml")["on"]
    assert "push" not in ci
    assert {"pull_request", "merge_group", "workflow_call"} <= set(ci)
    for workflow in ("agent-release.yml", "dev-images.yml", "installer-setups.yml"):
        called = load(WORKFLOWS / workflow)
        assert set(called["on"]) == {"workflow_call"}, workflow
        # The caller owns cancellation; an inner group would cancel jobs of
        # another release run.
        assert "concurrency" not in called
        assert all("concurrency" not in job for job in called["jobs"].values())


def test_every_called_workflow_gets_the_permissions_its_jobs_request() -> None:
    """A called job asking for more than its caller grants fails at startup."""
    jobs = load(RELEASE)["jobs"]
    for name, workflow in PRODUCERS.items():
        caller = jobs[name]
        assert caller["uses"] == f"./.github/workflows/{workflow}"
        granted = caller["permissions"]
        called = load(WORKFLOWS / workflow)
        for job in called["jobs"].values():
            requested = job.get("permissions", called["permissions"])
            for scope, level in requested.items():
                assert LEVEL[granted.get(scope, "none")] >= LEVEL[level], (
                    name,
                    scope,
                )


def test_publication_waits_for_every_producer_and_ci() -> None:
    jobs = load(RELEASE)["jobs"]
    authority = jobs["authority"]
    assert set(authority["needs"]) == {
        "changes",
        "ci",
        "agent",
        "apt",
        "images",
        "setups",
        "producers",
    }
    condition = " ".join(authority["if"].split())
    assert "!cancelled()" in condition
    assert "!contains(needs.*.result, 'failure')" in condition
    assert "!contains(needs.*.result, 'cancelled')" in condition
    assert "sleep" not in str(authority["steps"])
    assert jobs["candidate"]["needs"] == ["authority"]


def test_a_newer_main_push_cancels_work_but_never_an_r2_write() -> None:
    jobs = load(RELEASE)["jobs"]
    assert "concurrency" not in load(RELEASE)
    for name in (*PRODUCERS, "nas-lane-acceptance", "spark-acceptance", "acceptance"):
        concurrency = jobs[name]["concurrency"]
        assert "${{ github.ref }}" in concurrency["group"], name
        assert concurrency["cancel-in-progress"] == (
            "${{ github.ref == 'refs/heads/main' }}"
        ), name
    for name in R2_WRITERS:
        assert jobs[name]["concurrency"]["cancel-in-progress"] is False, name
    refresh = load(WORKFLOWS / "installer-maintenance.yml")["jobs"]["refresh"]
    assert refresh["concurrency"]["cancel-in-progress"] is False


def test_a_skipped_producer_does_not_skip_publication() -> None:
    # A job whose `if` has no status function gets an implicit `success()`,
    # which is false when any ancestor was skipped. Producers skip whenever
    # their inputs are unchanged, so every job downstream of them must use a
    # status function or it silently skips the whole publication.
    jobs = load(RELEASE)["jobs"]
    status = ("!cancelled()", "always()", "cancelled()", "failure()")
    downstream = {"authority"}
    changed = True
    while changed:
        changed = False
        for name, job in jobs.items():
            needs = job.get("needs", [])
            needs = [needs] if isinstance(needs, str) else needs
            if name not in downstream and downstream & set(needs):
                downstream.add(name)
                changed = True
    for name in sorted(downstream):
        condition = " ".join(str(jobs[name].get("if", "")).split())
        assert any(fn in condition for fn in status), name
