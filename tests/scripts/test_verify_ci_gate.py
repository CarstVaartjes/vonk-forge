from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    loader = importlib.machinery.SourceFileLoader(
        "verify_ci_gate", str(ROOT / "scripts/verify-ci-gate")
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
        "control-image-build": "success",
        "web": "success",
        "generated": "skipped",
        "compose": "success",
        "fault-recovery": "success",
        "nas-install": "success",
        "agent-package": "success",
        "lane-proof": "skipped",
        "supply-chain": "success",
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
        selected[area] = "false"
    for job in (
        "rust-quality",
        "rust-tests",
        "rust-platform",
        "controller-spark-wire",
        "repository",
        "control",
        "control-image-build",
        "web",
        "generated",
        "compose",
        "fault-recovery",
        "nas-install",
        "agent-package",
        "lane-proof",
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


@pytest.mark.parametrize("job", ["nas-install", "agent-package", "fault-recovery"])
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


@pytest.mark.parametrize("area", ("control", "compose"))
def test_fault_recovery_required_when_either_dependency_is_selected(area):
    selected, results = _valid()
    selected.update(control="false", compose="false")
    selected[area] = "true"
    results["fault-recovery"] = "skipped"
    assert any(
        "fault-recovery" in error
        for error in _module().verify("success", selected, results)
    )
