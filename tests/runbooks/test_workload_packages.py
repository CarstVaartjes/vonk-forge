"""Executable assertions for the workload-package operator contract."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[2]


def test_workload_runbook_covers_generic_lifecycle_and_recovery_boundaries() -> None:
    text = (ROOT / "docs/runbooks/workload-packages.md").read_text()
    required = (
        "family_id",
        "signed package",
        "Catalog and Library are the operator path for model recipes",
        "Candidate review and promotion",
        "Rollout and progress",
        "offline",
        "repair",
        "Garbage collection",
        "Credentials, licenses",
        "vonkctl admin packages",
        "vonkctl admin deployments",
        "outbound mTLS",
        "SSH is permitted only",
        "simulated",
        "physical",
    )
    for phrase in required:
        assert phrase in text


def test_top_level_docs_link_the_two_admin_surfaces_and_workload_runbook() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "docs/runbooks/workload-packages.md" in readme
    assert "vonkctl admin" in readme
    assert "web UX" in readme
    platform = (ROOT / "docs/runbooks/platform-update.md").read_text()
    assert "NAS-to-GPU node platform skew" in platform
    assert "outbound mTLS channel" in platform
    assert "workload package path remains independent" in platform


def test_first_release_plan_declares_independent_workload_evidence() -> None:
    plan = (ROOT / "docs/superpowers/plans/2026-08-03-platform-release-hardening.md").read_text()
    assert "workload-package-acceptance.json" in plan
    assert "workload-package-failure-matrix.json" in plan
    assert "release 2" in plan
    assert "offline" in plan
    assert "agent.update" in plan


def test_hosted_ci_uploads_independent_workload_evidence() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "workload-package-evidence" in workflow
    assert "scripts/accept-workload-packages --mode simulated --json" in workflow
    assert "scripts/accept-workload-package-failures" in workflow
    assert "workload-package-acceptance.json" in workflow
    assert "workload-package-failure-matrix.json" in workflow
    assert "actions/upload-artifact" in workflow
    evidence_job = workflow.split("  workload-package-evidence:", 1)[1].split(
        "  release-metadata:", 1
    )[0]
    assert "github.event_name == 'workflow_dispatch'" in evidence_job
    assert "github.ref_type == 'tag'" in evidence_job


def test_ci_only_runs_for_main_pull_requests_manual_dispatch_or_release_tags() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "  pull_request:\n    branches: [main]" in workflow
    assert "  workflow_dispatch:" in workflow
    assert '  push:\n    tags: ["v*"]' in workflow


def test_release_verifier_output_has_non_claiming_workload_defaults() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts/verify-platform-release"))
    report, missing = namespace["verify"](ROOT, "1.0.0")
    assert missing
    workload = report["workload_packages"]
    if any(gate.startswith("workload-package-acceptance") for gate in missing):
        assert workload["unknown_family_without_agent_update"] is False
        assert workload["release_two_activated"] is False
        assert workload["offline_release_one_rollback"] is False
        assert workload["unsigned_release_rejected"] is False
    else:
        # A preceding acceptance command may have intentionally left its
        # canonical report in the working tree; in that case the verifier
        # should reflect the observed evidence rather than call it missing.
        assert workload["unknown_family_without_agent_update"] is True
        assert workload["release_two_activated"] is True
        assert workload["offline_release_one_rollback"] is True
        assert workload["unsigned_release_rejected"] is True
    if any(gate.startswith("workload-package-failure-matrix") for gate in missing):
        assert workload["failure_matrix"] is False
    else:
        assert workload["failure_matrix"] is True
    if any(gate.startswith("workload-package-acceptance") for gate in missing):
        assert workload["ssh_calls"] is None
        assert workload["agent_update_calls"] is None
    else:
        assert workload["ssh_calls"] == 0
        assert workload["agent_update_calls"] == 0
    schema = json.loads((ROOT / "schemas/platform-release-evidence.schema.json").read_text())
    jsonschema.validate(report, schema)
