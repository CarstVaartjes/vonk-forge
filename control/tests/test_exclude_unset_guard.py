"""Persisted and wire state must retain values mutated outside field-set tracking.

Only explicitly constructed partial requests may be reviewed as exceptions.
Persistence, receipts, journals, events and complete wire state are never eligible.
The empty allowlist is a ratchet; stale or unmatched entries fail too.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from .parsed_sources import parse_file

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = ("control/src", "agent_protocol/src", "src")


def exclude_unset_sites(tree: ast.Module) -> list[tuple[str, str]]:
    """Return scope/expression identities, independent of formatting and lines."""
    sites: list[tuple[str, str]] = []

    class Visitor(ast.NodeVisitor):
        scope: tuple[str, ...] = ()

        def visit_scope(
            self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ) -> None:
            previous = self.scope
            self.scope = (*previous, node.name)
            self.generic_visit(node)
            self.scope = previous

        visit_ClassDef = visit_scope
        visit_FunctionDef = visit_scope
        visit_AsyncFunctionDef = visit_scope

        def visit_Call(self, node: ast.Call) -> None:
            for keyword in node.keywords:
                values = [(keyword.arg, keyword.value)]
                if keyword.arg is None and isinstance(keyword.value, ast.Dict):
                    values = [
                        (key.value, value)
                        for key, value in zip(
                            keyword.value.keys, keyword.value.values, strict=True
                        )
                        if isinstance(key, ast.Constant)
                    ]
                if any(
                    name == "exclude_unset"
                    and isinstance(value, ast.Constant)
                    and value.value is True
                    for name, value in values
                ):
                    sites.append((".".join(self.scope), ast.unparse(node)))
                    break
            self.generic_visit(node)

    Visitor().visit(tree)
    return sites


def test_exclude_unset_only_in_reviewed_partial_requests() -> None:
    allowance = json.loads((ROOT / "tools/exclude-unset-allowlist.json").read_text())
    assert allowance["schema"] == 1
    reviewed = allowance["sites"]
    assert len(reviewed) <= allowance["max_sites"]
    expected = []
    for entry in reviewed:
        assert entry["kind"] == "explicit-partial-request"
        assert entry["reason"].strip()
        assert entry["path"].startswith(tuple(f"{root}/" for root in SOURCE_ROOTS))
        expected.append((entry["path"], entry["function"], entry["expression"]))
    assert len(expected) == len(set(expected)), "duplicate serialization allowance"
    actual = []
    for source_root in SOURCE_ROOTS:
        for path in sorted((ROOT / source_root).rglob("*.py")):
            actual.extend(
                (path.relative_to(ROOT).as_posix(), scope, expression)
                for scope, expression in exclude_unset_sites(parse_file(path).tree)
            )
    assert sorted(actual) == sorted(expected), (
        "exclude_unset=True loses in-place state mutations; use the canonical serializer. "
        f"Unreviewed: {sorted(set(actual) - set(expected))}; "
        f"stale allowances: {sorted(set(expected) - set(actual))}"
    )


@pytest.mark.parametrize(
    ("source", "count"),
    [
        ("state.model_dump(exclude_unset = True)", 1),
        ("state.model_dump_json(exclude_unset=True)", 1),
        ('serialize(state, **{"exclude_unset": True})', 1),
        ("state.model_dump(exclude_unset=False)", 0),
        ('# exclude_unset=True\ntext = "exclude_unset=True"', 0),
    ],
)
def test_guard_detects_sparse_serialization_syntax(source: str, count: int) -> None:
    assert len(exclude_unset_sites(ast.parse(source))) == count
