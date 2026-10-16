"""The release workflow is one fan-in over CI and the development producers."""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
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


@pytest.mark.parametrize(
    ("ci_result", "tested_ci", "producer_result", "accepted"),
    [
        ("success", "", "success", True),
        ("skipped", "50", "success", True),
        ("skipped", "", "success", False),
        ("failure", "50", "success", False),
        ("cancelled", "50", "success", False),
        ("skipped", "50", "failure", False),
        ("skipped", "50", "cancelled", False),
    ],
)
def test_release_accepts_only_successful_or_proven_reused_ci(
    ci_result, tested_ci, producer_result, accepted
) -> None:
    """Exercise the actual gate: an unexplained skip must never publish."""
    expression = load(RELEASE)["jobs"]["authority"]["if"].strip()
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    for source, value in {
        "needs.*.result": repr([ci_result, producer_result]),
        "needs.ci.result": repr(ci_result),
        "needs.changes.outputs.tested_ci": repr(tested_ci),
        "needs.changes.outputs.channel": repr("dev"),
    }.items():
        expression = expression.replace(source, value)
    expression = re.sub(r"!(?!=)", "not ", expression)
    expression = expression.replace("&&", "and").replace("||", "or")
    assert (
        eval(
            " ".join(expression.split()),
            {"__builtins__": {}},
            {
                "cancelled": lambda: False,
                "contains": lambda values, item: item in values,
            },
        )
        is accepted
    )


def test_a_newer_main_push_queues_behind_a_running_release() -> None:
    # Catches a newer merge cancelling a release that is already running.
    jobs = load(RELEASE)["jobs"]
    assert "concurrency" not in load(RELEASE)
    for name in PRODUCERS:
        concurrency = jobs[name]["concurrency"]
        assert "${{ github.ref }}" in concurrency["group"], name
        assert concurrency["cancel-in-progress"] is False, name
    for name in ("spark-acceptance", "acceptance"):
        concurrency = load(ACCEPTANCE)["jobs"][name]["concurrency"]
        assert concurrency["cancel-in-progress"] is False
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


@pytest.mark.parametrize("role", ["api", "hermes", "litellm"])
def test_candidate_shared_builder_preserves_both_compose_image_identities(
    tmp_path: Path, role: str
) -> None:
    """Sharing the Controller builder must not drop or replace the worker digest."""
    jobs = load(WORKFLOWS / "release-acceptance.yml")["jobs"]
    writer = next(
        step["run"]
        for step in jobs["images"]["steps"]
        if step.get("name") == "Record content identity"
    )
    source = "a" * 40
    digest = "sha256:" + "b" * 64
    worker_digest = "sha256:" + "c" * 64
    environment = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "ROLE": role,
        "SOURCE": source,
        "DIGEST": digest,
        "WORKER_DIGEST": worker_digest if role == "api" else "",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
    }
    recorded = subprocess.run(
        ["bash", "-c", writer],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert recorded.returncode == 0, recorded.stderr
    identities = tmp_path / "image"
    assert (identities / role).read_text().strip() == (
        f"ghcr.io/carstvaartjes/vonk-forge-{role}:acceptance-{source}-123-2@{digest}"
    )
    if role != "api":
        assert not (identities / "worker").exists()
        return
    worker_image = (identities / "worker").read_text().strip()
    assert worker_image == (
        f"ghcr.io/carstvaartjes/vonk-forge-worker:acceptance-{source}-123-2@{worker_digest}"
    )
    # Exercise the real downstream assembly after merging per-job artifacts.
    identities.rename(tmp_path / "images")
    for auxiliary in ("hermes", "litellm"):
        (tmp_path / "images" / auxiliary).write_text(
            f"ghcr.io/carstvaartjes/vonk-forge-{auxiliary}:acceptance-{source}-123-2@{digest}\n"
        )
    assembly = next(
        step["run"]
        for step in jobs["compose"]["steps"]
        if step.get("name") == "Bind the built images by digest"
    )
    rendered = subprocess.run(
        ["bash", "-c", assembly],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    compose = yaml.safe_load(
        (tmp_path / "compose/docker-compose.pinned.yml").read_text()
    )
    assert compose["services"]["control-worker"]["image"] == worker_image
    assert (
        compose["services"]["control-api"]["image"]
        == (tmp_path / "images/api").read_text().strip()
    )


def test_pull_request_carry_follows_the_selector_and_the_release_always_carries() -> (
    None
):
    """Catches the release skipping the carry, a pull request carrying
    unconditionally again, or an acceptance signed over a carry that was
    requested but did not run."""
    ci = load(WORKFLOWS / "ci.yml")["jobs"]
    carry = ci["lane-proof"]["with"]["upgrade_carry"]
    assert "needs.changes.outputs.upgrade_carry == 'true'" in carry
    assert "'release-acceptance'" in carry
    assert (
        ci["changes"]["outputs"]["upgrade_carry"]
        == "${{ steps.areas.outputs.upgrade_carry }}"
    )
    pr = load(WORKFLOWS / "release-acceptance.yml")
    assert pr["on"]["workflow_call"]["inputs"]["upgrade_carry"]["default"] is True
    assert (
        pr["jobs"]["acceptance"]["with"]["upgrade_carry"]
        == "${{ inputs.upgrade_carry }}"
    )
    core = load(WORKFLOWS / "release-acceptance-core.yml")
    assert core["on"]["workflow_call"]["inputs"]["upgrade_carry"]["default"] is True
    jobs = core["jobs"]
    assert "inputs.upgrade_carry" in jobs["spark-upgrade-acceptance"]["if"]
    sign = jobs["acceptance"]["if"]
    assert "needs['spark-upgrade-acceptance'].result == 'success'" in sign
    assert (
        "!inputs.upgrade_carry && needs['spark-upgrade-acceptance'].result == 'skipped'"
        in sign
    )
    release = load(WORKFLOWS / "installer-publication.yml")["jobs"]
    callers = [
        job
        for job in release.values()
        if job.get("uses", "").endswith("release-acceptance-core.yml")
    ]
    assert callers
    for job in callers:
        assert "upgrade_carry" not in job.get("with", {})


def test_main_reuses_the_images_of_the_pull_request_that_tested_its_tree() -> None:
    """Catches the images producer ignoring the proving PR run, a build that
    still runs after reuse, or a consumer reading only a skipped build's
    digest (it would scan, accept or publish nothing)."""
    jobs = load(RELEASE)["jobs"]
    assert jobs["images"]["with"] == {
        "reuse_run_id": "${{ needs.changes.outputs.tested_ci }}"
    }
    images = load(WORKFLOWS / "dev-images.yml")
    assert images["on"]["workflow_call"]["inputs"]["reuse_run_id"]["default"] == ""
    steps = images["jobs"]["publish-development-images"]["steps"]
    by_id = {step.get("id"): step for step in steps}
    reuse = by_id["pr_images"]
    assert "scripts/reuse-pr-candidate-images" in reuse["run"]
    order = [step.get("id") for step in steps]
    for role in ("api", "worker", "hermes", "litellm"):
        build = by_id[f"build_{role}"]
        assert "steps.pr_images.outputs.reused != 'true'" in build["if"], role
        assert order.index("pr_images") < order.index(f"build_{role}")
    for role in ("hermes", "litellm"):
        assert (
            by_id[f"reuse_{role}"]["if"] == "steps.pr_images.outputs.reused != 'true'"
        )
    text = (WORKFLOWS / "dev-images.yml").read_text()
    for role in ("api", "worker", "hermes", "litellm"):
        bare = f"${{{{ steps.build_{role}.outputs.digest }}}}"
        assert bare not in text, role
        assert (
            f"steps.pr_images.outputs.{role}_digest || steps.build_{role}.outputs.digest"
            in text
        )


def test_a_newer_main_release_supersedes_only_through_the_bounded_script() -> None:
    """Catches the supersede job running for tags, gaining write access beyond
    cancelling runs, or its commit markers drifting from the job names that
    write apt dev or start publication (a rename would let it cancel those)."""
    jobs = load(RELEASE)["jobs"]
    supersede = jobs["supersede"]
    assert supersede["if"] == "github.ref == 'refs/heads/main'"
    assert supersede["permissions"] == {"actions": "write", "contents": "read"}
    assert "needs" not in supersede
    assert "scripts/supersede-release-runs" in supersede["steps"][-1]["run"]
    loader = importlib.machinery.SourceFileLoader(
        "supersede_release_runs", str(ROOT / "scripts/supersede-release-runs")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    assert set(module.COMMITTED_JOBS) == {
        jobs["apt"]["name"],
        jobs["authority"]["name"],
    }
    assert jobs["candidate"]["needs"] == ["authority"]
    assert {"authority", "candidate"} <= set(jobs["promote"]["needs"])
