"""The release workflow is one fan-in over CI and the development producers."""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
RELEASE = WORKFLOWS / "installer-publication.yml"
ACCEPTANCE = WORKFLOWS / "release-acceptance-core.yml"
CANDIDATE = WORKFLOWS / "installer-candidate.yml"
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


def test_pr_acceptance_grants_permissions_through_reusable_calls() -> None:
    """Catches a PR caller reducing grants needed by isolated candidate jobs."""
    for caller_path, job_name, called_path in (
        ("ci.yml", "lane-proof", "release-acceptance.yml"),
        ("release-acceptance.yml", "candidate", "installer-candidate.yml"),
        ("release-acceptance.yml", "acceptance", "release-acceptance-core.yml"),
        ("release-acceptance.yml", "setups", "installer-setups.yml"),
    ):
        caller = load(WORKFLOWS / caller_path)
        job = caller["jobs"][job_name]
        granted = job.get("permissions", caller["permissions"])
        called = load(WORKFLOWS / called_path)
        for requested in (
            called["permissions"],
            *(
                job.get("permissions", called["permissions"])
                for job in called["jobs"].values()
            ),
        ):
            for scope, level in requested.items():
                assert LEVEL[granted.get(scope, "none")] >= LEVEL[level], (
                    caller_path,
                    job_name,
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
    for name in PRODUCERS:
        concurrency = jobs[name]["concurrency"]
        assert "${{ github.ref }}" in concurrency["group"], name
        assert concurrency["cancel-in-progress"] == (
            "${{ github.ref == 'refs/heads/main' }}"
        ), name
    for name in ("spark-acceptance", "acceptance"):
        concurrency = load(ACCEPTANCE)["jobs"][name]["concurrency"]
        assert (
            concurrency["cancel-in-progress"]
            == "${{ github.ref == 'refs/heads/main' }}"
        )
    assert (
        load(ACCEPTANCE)["jobs"]["nas-lane-acceptance"]["concurrency"][
            "cancel-in-progress"
        ]
        is False
    )
    for name in R2_WRITERS:
        job = load(CANDIDATE)["jobs"][name] if name == "candidate" else jobs[name]
        assert job["concurrency"]["cancel-in-progress"] is False, name
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


def test_acceptance_failure_reports_upload_without_forgiving_the_failed_run() -> None:
    """A failed acceptance step must still upload diagnostics and block signing."""
    for workflow, gate_jobs in (
        (ACCEPTANCE, ("nas-acceptance", "spark-acceptance")),
        (WORKFLOWS / "spark-upgrade-acceptance.yml", ("carry",)),
    ):
        jobs = load(workflow)["jobs"]
        for name in gate_jobs:
            steps = jobs[name]["steps"]
            uploads = [
                (index, step)
                for index, step in enumerate(steps)
                if "actions/upload-artifact@" in step.get("uses", "")
                and "report" in step.get("name", "").lower()
            ]
            assert uploads, f"{name} lost its failure report upload"
            for index, step in uploads:
                assert step["if"] == "always()"
                runners = [
                    item for item in steps[:index] if "report" in item.get("run", "")
                ]
                assert runners
                assert all(not item.get("continue-on-error", False) for item in runners)
    signing = load(ACCEPTANCE)["jobs"]["acceptance"]
    for job in ("nas-acceptance", "spark-acceptance", "spark-upgrade-acceptance"):
        assert f"needs['{job}'].result == 'success'" in signing["if"]


def test_pr_and_release_share_the_candidate_and_acceptance_jobs() -> None:
    # A caller accidentally inlining a lane or accepting the promoted release
    # would break exact-source parity before its broken harness reaches main.
    release = load(RELEASE)["jobs"]
    pr = load(WORKFLOWS / "release-acceptance.yml")["jobs"]
    for jobs, acceptance in ((release, "release-acceptance"), (pr, "acceptance")):
        assert (
            jobs["candidate"]["uses"] == "./.github/workflows/installer-candidate.yml"
        )
        assert (
            jobs[acceptance]["uses"]
            == "./.github/workflows/release-acceptance-core.yml"
        )
        assert (
            "needs.candidate.outputs.generation"
            in jobs[acceptance]["with"]["generation"]
        )
    assert "promote" not in pr
    assert "release-acceptance" in release["promote"]["needs"]
    assert pr["candidate"]["with"]["premerge"] is True


def test_fast_jobs_have_no_path_predicate_and_gate_observes_acceptance() -> None:
    jobs = load(WORKFLOWS / "ci.yml")["jobs"]
    for name in (
        "rust-quality",
        "rust-tests",
        "generated-clients",
        "repository-suite",
        "consumer-probe",
        "control-suite",
        "web-suite",
        "repository-guards",
        "lint",
    ):
        assert "if" not in jobs[name], name
    assert "lane-proof" in jobs["ci-gate"]["needs"]
    # Integration PRs prove their own complete candidate with no release secrets.
    assert jobs["lane-proof"]["uses"] == "./.github/workflows/release-acceptance.yml"
    assert jobs["lane-proof"]["with"]["ref"] == "${{ github.sha }}"


def test_pr_candidates_cannot_acquire_production_publication_authority() -> None:
    """Catches a PR path requesting main-only signing or R2 credentials."""
    pr = load(WORKFLOWS / "release-acceptance.yml")["jobs"]
    assert all("environment" not in job for job in pr.values())
    assert all("secrets" not in job for job in pr.values())
    for workflow, jobs in (
        ("installer-candidate.yml", ("candidate",)),
        (
            "release-acceptance-core.yml",
            ("nas-lane-acceptance", "spark-acceptance", "acceptance"),
        ),
        ("spark-upgrade-acceptance.yml", ("carry",)),
    ):
        document = load(WORKFLOWS / workflow)
        for name in jobs:
            environment = document["jobs"][name]["environment"]
            assert "inputs.premerge && format('ephemeral-acceptance-" in environment
    candidate = load(CANDIDATE)["jobs"]["candidate"]
    for step in candidate["steps"]:
        if "R2 publication client" in step.get(
            "name", ""
        ) or "Publish immutable" in step.get("name", ""):
            assert step["if"] == "${{ !inputs.premerge }}"
    assert pr["setups"]["with"]["test_trust"] is True
    assert "openssl genpkey -algorithm ED25519" in str(pr["package"]["steps"])
    assert "secrets." not in str(pr)

    assert "head.repo.full_name == github.repository" in pr["source"]["if"]
    ci = load(WORKFLOWS / "ci.yml")["jobs"]
    assert "head.repo.full_name == github.repository" in ci["lane-proof"]["if"]
    setup = load(WORKFLOWS / "installer-setups.yml")["jobs"]["build-and-test"]
    build = next(
        step
        for step in setup["steps"]
        if step.get("name") == "Test and build exact native setup programs"
    )
    assert '"$TEST_TRUST" == true' in build["run"]
    assert (
        'cp "$RUNNER_TEMP/ephemeral-public/key.pem" install/installer-release-public.pem'
        in build["run"]
    )
    assert "acceptance-test-trust" not in build["run"]
    assert "ephemeral-installer-private" in str(candidate["steps"])


def test_pr_image_references_pass_the_publication_ingress_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a publisher that mistakes run-scoped tags for content identity."""
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    loader = importlib.machinery.SourceFileLoader(
        "install_release_publication", str(ROOT / "scripts/install-release-publication")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    publication = importlib.util.module_from_spec(spec)
    loader.exec_module(publication)
    for role in ("api", "worker", "hermes", "litellm"):
        reference = (
            f"ghcr.io/carstvaartjes/vonk-forge-{role}:acceptance-{'b' * 40}-123-1"
            f"@sha256:{'a' * 64}"
        )
        publication._validate_image(reference, role)
        with pytest.raises(publication.PublicationError):
            publication._validate_image(reference.split("@")[0], role)
        # An invalid ingress reference leaves no state blocking a fresh one.
        publication._validate_image(reference, role)


def test_ephemeral_publication_leaves_controller_listener_available() -> None:
    """Catches publication occupying the port needed to boot its candidate."""
    action = ROOT / ".github/actions/ephemeral-publication/action.yml"
    origins = set(re.findall(r"https://localhost:\d+", action.read_text()))
    assert len(origins) == 1
    origin = origins.pop()
    publication_port = urlsplit(origin).port
    compose = yaml.safe_load((ROOT / "deploy/compose/compose.yaml").read_text())
    controller_ports = compose["services"]["caddy"]["ports"]
    assert publication_port not in {int(port["published"]) for port in controller_ports}
    server = ast.parse((ROOT / "scripts/serve-acceptance-objects").read_text())
    defaults = [
        keyword.value.value
        for call in ast.walk(server)
        if isinstance(call, ast.Call)
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "--port"
        for keyword in call.keywords
        if keyword.arg == "default" and isinstance(keyword.value, ast.Constant)
    ]
    assert defaults == [publication_port]
    for path in (
        CANDIDATE,
        ACCEPTANCE,
        WORKFLOWS / "spark-upgrade-acceptance.yml",
        ROOT / "tests/acceptance/test_spark_lifecycle.py",
    ):
        assert set(re.findall(r"https://localhost:\d+", path.read_text())) == {origin}
