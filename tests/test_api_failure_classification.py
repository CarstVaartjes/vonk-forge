"""Reject broad deterministic-exception handlers that declare a retryable outage.

The connected fault-injection guard lives with the Controller implementation in
control/tests/test_api_failure_classification.py. This scan prevents new direct
503 mappings; it does not infer arbitrary helper control flow.
"""

import ast
from pathlib import Path

from vonk_agent_protocol import ArtifactLifecycleCode, ModelCacheCode

from control.tests.parsed_sources import parse_file

ROOT = Path(__file__).resolve().parents[1]


def _deterministic_outages(tree: ast.Module) -> list[int]:
    violations = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
            continue
        caught = {
            node.id for node in ast.walk(handler.type) if isinstance(node, ast.Name)
        }
        if not caught & {
            "TypeError",
            "ValueError",
            "RuntimeError",
            "Exception",
            "BaseException",
        }:
            continue
        for node in ast.walk(handler):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name not in {"HTTPException", "Response", "JSONResponse"}:
                continue
            status = [
                keyword.value
                for keyword in node.keywords
                if keyword.arg == "status_code"
            ]
            if name == "HTTPException" and node.args:
                status.append(node.args[0])
            if any(
                isinstance(value, ast.Constant) and value.value == 503
                for value in status
            ):
                violations.append(handler.lineno)
    return violations


def test_api_handlers_do_not_mask_deterministic_faults_as_outages():
    """Catch a broad except -> generic 503 anywhere in the Controller source."""
    violations = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in (ROOT / "control/src").rglob("*.py")
        for line in _deterministic_outages(parse_file(path).tree)
    ]
    assert not violations, violations


def test_outage_scan_distinguishes_deterministic_faults_from_io():
    """The scan must reject direct broad mappings and permit a genuine I/O wait."""
    tree = ast.parse("""
try:
    review()
except (TypeError, ValueError):
    raise HTTPException(status_code=503, detail="unavailable") from None
try:
    read()
except OSError:
    raise HTTPException(503, "unavailable") from None
try:
    observe()
except Exception:
    return JSONResponse(status_code=503, content={})
""")
    assert _deterministic_outages(tree) == [4, 12]


# These describe missing observations, resumable transfers or a temporary owner.
# Any other immutable cache condition needs a typed invalid/conflict/refusal.
_CACHE_OBSERVATIONS = frozenset(
    {
        ModelCacheCode.SOURCE_UNAVAILABLE,
        ModelCacheCode.RECIPE_REVISION_MISSING,
        ModelCacheCode.RECIPE_IDENTITY_MISSING,
        ModelCacheCode.MODEL_DEFINITION_MISSING,
        ModelCacheCode.ENTRY_MISSING,
        ModelCacheCode.COVERAGE_INCOMPLETE,
        ModelCacheCode.ARTIFACT_UNVERIFIED,
        ModelCacheCode.SOURCE_TRUNCATED,
        ModelCacheCode.SOURCE_SIZE_MISMATCH,
        ModelCacheCode.RATE_LIMITED,
        ModelCacheCode.RANGE_INVALID,
    }
)


def _cache_deterministic_observations(tree: ast.Module) -> list[int]:
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if not (
            node.func.id.startswith("ModelCache") and node.func.id.endswith("Unknown")
        ):
            continue
        if not node.args or not isinstance(node.args[0], ast.Attribute):
            continue
        code = node.args[0]
        if not isinstance(code.value, ast.Name):
            continue
        if code.value.id == "ModelCacheCode":
            deterministic = ModelCacheCode[code.attr] not in _CACHE_OBSERVATIONS
        else:
            deterministic = (
                code.value.id == "ArtifactLifecycleCode"
                and ArtifactLifecycleCode[code.attr]
                in {
                    ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH,
                    ArtifactLifecycleCode.DELETION_FENCE_LOST,
                }
            )
        if deterministic:
            violations.append(node.lineno)
    return violations


def test_cache_deterministic_errors_are_not_observation_waits():
    """Catch deterministic cache errors sent through the retryable error class."""
    violations = [
        f"{path.relative_to(ROOT)}:{line}"
        for path in (ROOT / "control/src/vonk_control/model_cache").glob("*.py")
        for line in _cache_deterministic_observations(parse_file(path).tree)
    ]
    assert not violations, violations


def test_cache_observation_guard_rejects_conflicts_but_permits_unavailable_sources():
    tree = ast.parse("""
raise ModelCacheStorageUnknown(ModelCacheCode.SELECTOR_AMBIGUOUS, "choose digest")
raise ModelCacheStorageUnknown(ModelCacheCode.SOURCE_UNAVAILABLE, "observe")
raise ModelCacheStorageUnknown(ModelCacheCode.MODEL_DEPENDENCY_CYCLE, "invalid")
raise ModelCacheConflictUnknown(ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH, "replan")
raise ModelCacheConflictUnknown(ArtifactLifecycleCode.DELETION_FENCE_LOST, "replan")
raise ModelCacheConflictUnknown(ArtifactLifecycleCode.REFERENCE_UNAVAILABLE, "observe")
""")
    assert _cache_deterministic_observations(tree) == [2, 4, 5, 6]
