"""A submit or create request never escalates into a Stop (its Spark ordinal bump)."""

import ast
from pathlib import Path

SERVICE = Path("control/src/vonk_control/artifact_jobs/service.py")
FORBIDDEN = {"preview_stop", "stop", "_reconcile_run_for_submission"}


def forbidden_call_lines(source: str) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN)
            or (isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN)
        )
    ]


def test_artifact_job_requests_never_call_a_stop():
    """Catches a submit stopping the run to clear an unobservable previous job."""
    assert forbidden_call_lines(SERVICE.read_text()) == []


def test_the_scan_sees_the_calls_it_forbids():
    """Catches a scan that matches nothing."""
    assert forbidden_call_lines(
        "self._recipe_operations.stop(run_id)\npreview_stop(r)\n"
    ) == [
        1,
        2,
    ]
