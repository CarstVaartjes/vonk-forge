"""A serving chat check is sent only through the one helper that fits it to the facts."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("src", "scripts", "control/src", "tests/acceptance")
#: Every reader of a recipe's declared serving checks, and why it needs no fitting.
CONSUMERS = {
    "control/src/vonk_control/recipe_packages/__init__.py": "checks package file closure; sends nothing",
    "scripts/validate-recipe-library": "checks package file closure; sends nothing",
    "scripts/qualify-recipe": "sends through execute_http_check with the recipe's chat facts",
    "tests/acceptance/test_spark_lifecycle.py": "synthetic fixture canary; its recipe publishes no reasoning",
}


def _sources() -> list[Path]:
    return [
        path
        for root in SCANNED
        for path in (ROOT / root).rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix in {".py", ""}
    ]


def test_serving_checks_have_only_reviewed_readers() -> None:
    readers = {
        path.relative_to(ROOT).as_posix()
        for path in _sources()
        if "validation.serving.checks"
        in path.read_text(encoding="utf-8", errors="ignore")
    }
    assert readers == set(CONSUMERS), (
        "a new reader of validation.serving.checks must send its chat request through "
        "cluster_profiles.serving_request.bounded_chat_check (execute_http_check does) "
        "and be listed here"
    )


def test_execute_http_check_fits_the_check_before_it_builds_the_request() -> None:
    source = (ROOT / "src/cluster_profiles/serving_execution.py").read_text()
    function = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "execute_http_check"
    )
    lines = {
        name: min(
            call.lineno
            for call in ast.walk(function)
            if isinstance(call, ast.Call) and ast.unparse(call.func).endswith(name)
        )
        for name in ("bounded_chat_check", "urllib.request.Request")
    }
    assert lines["bounded_chat_check"] < lines["urllib.request.Request"]


def test_the_qualifier_passes_the_recipes_chat_facts_to_the_sender() -> None:
    source = (ROOT / "scripts/qualify-recipe").read_text()
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "execute_http_check"
    ]
    assert calls
    assert all(
        "chat_api" in {keyword.arg for keyword in call.keywords} for call in calls
    )
