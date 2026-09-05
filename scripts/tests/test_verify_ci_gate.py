from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

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
        "rust": "true", "repository": "false", "control": "true", "web": "true",
        "generated": "false", "compose": "true",
    }
    results = {
        "lint": "success",
        "rust": "success",
        "repository": "skipped",
        "control": "success",
        "web": "success",
        "generated": "skipped",
        "compose": "success",
        "catalog-runtime": "success",
    }
    selected.update({key: value for key, value in overrides.items() if key in selected})
    results.update({key: value for key, value in overrides.items() if key in results})
    return selected, results


def test_selected_jobs_must_succeed_and_unselected_jobs_may_skip() -> None:
    selected, results = _valid()
    assert _module().verify("success", selected, results) == []


def test_rejects_selector_failure_and_unexpected_skip() -> None:
    selected, results = _valid()
    results["rust"] = "skipped"
    errors = _module().verify("failure", selected, results)
    assert any("selector result" in error for error in errors)
    assert any("rust result" in error for error in errors)


def test_rejects_failure_cancelled_and_unexpected_success() -> None:
    selected, results = _valid()
    results["generated"] = "success"
    results["compose"] = "cancelled"
    errors = _module().verify("success", selected, results)
    assert any("generated result" in error for error in errors)
    assert any("compose result" in error for error in errors)
