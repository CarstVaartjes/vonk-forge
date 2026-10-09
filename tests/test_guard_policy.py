"""Guard exceptions name identities and reasons, never numerical allowances."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEDGER_KEYS = frozenset({"count", "max_sites", "max_debt", "ceiling", "debt_ceiling"})


def numerical_allowances(document: object) -> list[str]:
    if isinstance(document, dict):
        return [
            str(key)
            for key, value in document.items()
            if key in LEDGER_KEYS and isinstance(value, (int, float))
        ] + [key for value in document.values() for key in numerical_allowances(value)]
    if isinstance(document, list):
        return [key for value in document for key in numerical_allowances(value)]
    return []


def test_guard_registries_have_no_numerical_allowances() -> None:
    paths = {
        *ROOT.joinpath("tools").glob("*allowlist.json"),
        *ROOT.joinpath("tools").glob("*baseline.json"),
    }
    offenders = {
        path.name: keys
        for path in sorted(paths)
        if (keys := numerical_allowances(json.loads(path.read_text())))
    }
    assert not offenders, offenders


@pytest.mark.parametrize("key", sorted(LEDGER_KEYS))
def test_guard_rejects_a_numerical_exception_budget(key: str) -> None:
    assert numerical_allowances({"exceptions": [{key: 2, "reason": "reviewed"}]})


def test_guard_accepts_named_exceptions_and_product_resource_limits() -> None:
    assert not numerical_allowances(
        {
            "exceptions": [
                {
                    "file": "source.py",
                    "function": "consume",
                    "reason": "external passthrough",
                }
            ],
            "max_bytes": 1024,
            "timeout_seconds": 30,
        }
    )


def documentation_reads(source: str) -> list[int]:
    """Find repository documentation reads, including path aliases."""
    if not any(marker in source for marker in ("docs/", "README.md", "AGENTS.md")):
        return []
    tree = ast.parse(source)
    aliases = {
        target.id: node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    documented_paths: set[str] = set()

    def is_documentation(node: ast.AST) -> bool:
        return any(
            (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and (
                    child.value.startswith("docs/")
                    or child.value in {"README.md", "AGENTS.md"}
                )
            )
            or (isinstance(child, ast.Name) and child.id in documented_paths)
            for child in ast.walk(node)
        )

    for _ in aliases:
        discovered = {
            name for name, value in aliases.items() if is_documentation(value)
        }
        if discovered <= documented_paths:
            break
        documented_paths.update(discovered)

    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"read_text", "read_bytes", "open"}
        and is_documentation(node.func.value)
    ]


def test_tests_do_not_assert_repository_documentation_content() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): lines
        for suite in ("tests", "control/tests", "agent_protocol/tests")
        for path in ROOT.joinpath(suite).rglob("*.py")
        if (lines := documentation_reads(path.read_text()))
    }
    assert not offenders, offenders


@pytest.mark.parametrize(
    "source",
    [
        'text = (ROOT / "docs/runbooks/vonkctl.md").read_text()',
        'path = ROOT / "README.md"\ntext = path.read_text()',
    ],
)
def test_guard_rejects_documentation_content_reads(source: str) -> None:
    assert documentation_reads(source)


def test_guard_preserves_owned_document_fixtures() -> None:
    assert not documentation_reads('text = (tmp_path / "document.md").read_text()')
