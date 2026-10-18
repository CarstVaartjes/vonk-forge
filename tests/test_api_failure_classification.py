"""Reject broad deterministic-exception handlers that declare a retryable outage.

The connected fault-injection guard lives with the Controller implementation in
control/tests/test_api_failure_classification.py. This scan prevents new direct
503 mappings; it does not infer arbitrary helper control flow.
"""

import ast
from pathlib import Path

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
