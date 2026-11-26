from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _module():
    loader = importlib.machinery.SourceFileLoader(
        "verify_ci_gate", str(ROOT / "scripts/ci/verify-ci-gate")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _valid(**overrides: str):
    selected = {
        "rust": "true",
        "repository": "false",
        "control": "true",
        "web": "true",
        "generated": "false",
        "compose": "true",
        "nas-install": "true",
        "agent-package": "true",
        "lane": "false",
        "full": "true",
        "rust-changed": "false",
    }
    results = {
        "lint": "success",
        "repository-guards": "success",
        "rust-quality": "success",
        "rust-tests": "success",
        "rust-platform": "success",
        "controller-spark-wire": "success",
        "repository": "skipped",
        "control": "success",
        "corpus-acceptance": "success",
        "control-image-build": "success",
        "web": "success",
        "generated": "skipped",
        "compose": "success",
        "nas-install": "success",
        "agent-package": "success",
        "lane-proof": "skipped",
        "supply-chain": "success",
        "consumer-probe": "success",
        "member-web": "skipped",
        "client-compatibility-report": "success",
        "installed-cli-proof": "success",
        "model-cache-unknown-expiry": "success",
        "oci-link-fallback": "success",
        "privileged-helper-oci-process": "success",
        "profile-effect-consumer": "success",
    }
    selected.update({key: value for key, value in overrides.items() if key in selected})
    results.update({key: value for key, value in overrides.items() if key in results})
    return selected, results


def test_selected_jobs_must_succeed_and_unselected_jobs_may_skip() -> None:
    selected, results = _valid()
    assert _module().verify("success", selected, results) == []


def test_docs_only_change_allows_unselected_jobs_to_skip() -> None:
    selected, results = _valid()
    for area in selected:
        if area != "full":
            selected[area] = "false"
    for job in (
        "rust-quality",
        "rust-tests",
        "rust-platform",
        "controller-spark-wire",
        "repository",
        "control",
        "corpus-acceptance",
        "control-image-build",
        "web",
        "generated",
        "compose",
        "nas-install",
        "agent-package",
        "lane-proof",
        "client-compatibility-report",
        "installed-cli-proof",
        "model-cache-unknown-expiry",
        "oci-link-fallback",
        "privileged-helper-oci-process",
        "profile-effect-consumer",
    ):
        results[job] = "skipped"
    assert _module().verify("success", selected, results) == []


def test_rejects_selector_failure_and_unexpected_skip() -> None:
    selected, results = _valid()
    results["rust-quality"] = "skipped"
    errors = _module().verify("failure", selected, results)
    assert any("selector result" in error for error in errors)
    assert any("rust-quality result" in error for error in errors)


def test_supply_chain_verification_is_required() -> None:
    selected, results = _valid()
    results["supply-chain"] = "failure"
    errors = _module().verify("success", selected, results)
    assert errors == ["supply-chain result is 'failure', expected 'success'"]


def test_rejects_failure_cancelled_and_unexpected_success() -> None:
    selected, results = _valid()
    results["generated"] = "success"
    results["compose"] = "cancelled"
    errors = _module().verify("success", selected, results)
    assert any("generated result" in error for error in errors)
    assert any("compose result" in error for error in errors)


@pytest.mark.parametrize("area", ["rust", "control"])
@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", None])
def test_wire_contract_is_required_for_either_language_change(area, result) -> None:
    selected, results = _valid()
    selected.update(rust="false", control="false")
    selected[area] = "true"
    results["controller-spark-wire"] = result
    assert any(
        "controller-spark-wire" in error
        for error in _module().verify("success", selected, results)
    )


def test_controller_image_build_tests_are_required_with_the_control_suite() -> None:
    selected, results = _valid()
    results["control-image-build"] = "skipped"
    assert _module().verify("success", selected, results) == [
        "control-image-build result is 'skipped', expected 'success'"
    ]


def test_test_only_control_change_keeps_tests_without_rebuilding_images() -> None:
    # Catches treating a successful source suite as requiring unchanged images
    # to be rebuilt, while still refusing a failed selected source suite.
    selected, results = _valid()
    selected["nas-install"] = "false"
    results["nas-install"] = "skipped"
    results["control-image-build"] = "skipped"
    assert _module().verify("success", selected, results) == []
    results["control"] = "failure"
    assert _module().verify("success", selected, results) == [
        "control result is 'failure', expected 'success'"
    ]


@pytest.mark.parametrize("job", ["nas-install", "agent-package"])
@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", None])
def test_installed_system_proofs_block_the_gate_when_selected(job, result) -> None:
    selected, results = _valid()
    results[job] = result
    assert any(job in error for error in _module().verify("success", selected, results))


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", None])
def test_a_lane_change_needs_a_green_lane_run_on_its_head(result) -> None:
    selected, results = _valid()
    selected["lane"] = "true"
    results["lane-proof"] = "success"
    assert _module().verify("success", selected, results) == []
    results["lane-proof"] = result
    assert any(
        "lane-proof" in error
        for error in _module().verify("success", selected, results)
    )


def test_an_unrelated_change_does_not_wait_for_the_lane() -> None:
    selected, results = _valid()
    results["lane-proof"] = "success"
    assert any(
        "lane-proof" in error
        for error in _module().verify("success", selected, results)
    )


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", None])
def test_repository_guards_are_required_when_heavy_repository_is_skipped(
    result,
) -> None:
    selected, results = _valid()
    assert selected["repository"] == "false"
    results["repository-guards"] = result
    assert any(
        "repository-guards" in error
        for error in _module().verify("success", selected, results)
    )


def test_member_pull_request_runs_only_the_quick_tier() -> None:
    selected, results = _valid(full="false")
    for job in results:
        results[job] = "skipped"
    results.update(lint="success", **{"member-web": "success"})
    assert _module().verify("success", selected, results) == []
    # Any full-suite job that ran anyway is refused.
    refused = dict(results, **{"rust-tests": "success"})
    assert _module().verify("success", selected, refused) == [
        "rust-tests result is 'success', expected 'skipped'"
    ]
    # The quick tier is not optional.
    missing = dict(results, **{"member-web": "skipped"})
    assert _module().verify("success", selected, missing) == [
        "member-web result is 'skipped', expected 'success'"
    ]


@pytest.mark.parametrize("rust_changed", ["true", "false"])
def test_member_rust_quality_runs_only_when_rust_changed(rust_changed) -> None:
    selected, results = _valid(full="false")
    selected["rust-changed"] = rust_changed
    for job in results:
        results[job] = "skipped"
    results.update(lint="success", **{"member-web": "success"})
    ran = rust_changed == "true"
    results["rust-quality"] = "success" if ran else "skipped"
    assert _module().verify("success", selected, results) == []
    results["rust-quality"] = "skipped" if ran else "success"
    assert any(
        "rust-quality" in e for e in _module().verify("success", selected, results)
    )


def test_missing_scope_selection_is_refused() -> None:
    selected, results = _valid()
    del selected["full"]
    assert "full selection is None, expected 'true' or 'false'" in _module().verify(
        "success", selected, results
    )


def test_consumer_probe_verification_is_required() -> None:
    selected, results = _valid()
    results["consumer-probe"] = "failure"
    errors = _module().verify("success", selected, results)
    assert errors == ["consumer-probe result is 'failure', expected 'success'"]


def test_folded_proofs_follow_their_area() -> None:
    selected, results = _valid()
    results["oci-link-fallback"] = "skipped"
    assert _module().verify("success", selected, results) == [
        "oci-link-fallback result is 'skipped', expected 'success'"
    ]


def _gate_needs() -> list[str]:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    return ci["jobs"]["ci-gate"]["needs"]


def _needs_document(selected: dict[str, str], results: dict[str, str]) -> str:
    """The `toJSON(needs)` the gate receives, built from ci.yml's real needs."""
    keys = _module().JOB_KEYS
    document = {
        job: {"result": results[keys.get(job, job)], "outputs": {}}
        for job in _gate_needs()
        if job != "changes"
    }
    outputs = {key.replace("-", "_"): value for key, value in selected.items()}
    document["changes"] = {"result": "success", "outputs": outputs}
    return json.dumps(document)


def _refusals(document: str) -> list[str]:
    selector_result, selected, results, errors = _module().from_needs(document)
    return errors + _module().verify(selector_result, selected, results)


def _member_selection() -> tuple[dict[str, str], dict[str, str]]:
    selected, results = _valid(full="false")
    results = dict.fromkeys(results, "skipped")
    results.update(lint="success", **{"member-web": "success"})
    return selected, results


@pytest.mark.parametrize("tier", ["full", "member"])
def test_every_gated_job_failing_is_refused(tier: str) -> None:
    selected, results = _valid() if tier == "full" else _member_selection()
    assert _refusals(_needs_document(selected, results)) == []
    keys = _module().JOB_KEYS
    for job in _gate_needs():
        if job == "changes":
            continue
        failed = dict(results, **{keys.get(job, job): "failure"})
        assert _refusals(_needs_document(selected, failed)), job
    document = json.loads(_needs_document(selected, results))
    document["changes"]["result"] = "failure"
    assert _refusals(json.dumps(document))


def test_a_gated_job_without_a_rule_is_refused() -> None:
    selected, results = _valid()
    document = json.loads(_needs_document(selected, results))
    document["brand-new-proof"] = {"result": "success", "outputs": {}}
    [refusal] = _refusals(json.dumps(document))
    assert "brand-new-proof" in refusal


def test_a_job_sharing_a_verdict_key_cannot_mask_a_failure() -> None:
    selected, results = _valid()
    document = json.loads(_needs_document(selected, results))
    document["web-suite"]["result"] = "failure"
    document["web"] = {"result": "success", "outputs": {}}
    assert _refusals(json.dumps(document))


def test_the_gate_step_hands_it_the_needs_json() -> None:
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    [step] = [
        step
        for step in ci["jobs"]["ci-gate"]["steps"]
        if "scripts/ci/verify-ci-gate" in step.get("run", "")
    ]
    assert step["env"]["NEEDS"] == "${{ toJSON(needs) }}"
    assert step["run"].split() == ["scripts/ci/verify-ci-gate", "--needs-env", "NEEDS"]
