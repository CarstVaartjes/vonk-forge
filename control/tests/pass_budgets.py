"""Find a pass budget that setup spends before the pass reaches its work.

A bounded pass (``deadline = time.monotonic() + BUDGET``) must reach at least
one unit of work. Opening a session, lock or other context between starting the
budget and first reading it charges fixed setup (an inventory, a page query) to
the budget; when that setup alone takes the budget, every pass ends before its
first unit, forever and silently. Start the budget after the setup instead.
A context entered inside the pass's loop is its work (a retry's attempt), not
setup, so the scan ends at the first loop or the first read of the budget.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator

KIND = "pass-budget-spent-on-setup"

_MONOTONIC = re.compile(r"\bmonotonic\(\)$")


def _budget_name(node: ast.stmt) -> str | None:
    """The name a ``name = <monotonic>() + budget`` statement starts, if any."""
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.BinOp)
        and isinstance(node.value.op, ast.Add)
        and _MONOTONIC.search(ast.unparse(node.value.left))
    ):
        return node.targets[0].id
    return None


def _in_order(body: list[ast.stmt]) -> Iterator[tuple[ast.stmt, list[ast.AST]]]:
    """Each statement with the parts it evaluates before its nested body runs."""
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        if isinstance(node, ast.With | ast.AsyncWith):
            yield node, [item.context_expr for item in node.items]
            yield from _in_order(node.body)
        elif isinstance(node, ast.For | ast.AsyncFor):
            yield node, [node.iter]
            yield from _in_order(node.body + node.orelse)
        elif isinstance(node, ast.While | ast.If):
            yield node, [node.test]
            yield from _in_order(node.body + node.orelse)
        elif isinstance(node, ast.Try | ast.TryStar):
            yield node, []
            yield from _in_order(node.body)
            for handler in node.handlers:
                yield from _in_order(handler.body)
            yield from _in_order(node.orelse + node.finalbody)
        else:
            yield node, [node]


def _reads(parts: list[ast.AST], name: str) -> bool:
    return any(
        isinstance(node, ast.Name)
        and node.id == name
        and isinstance(node.ctx, ast.Load)
        for part in parts
        for node in ast.walk(part)
    )


def setup_spent_budget_lines(tree: ast.Module) -> Iterator[tuple[str, int]]:
    """(function, line) of each budget started before a context it does not bound."""
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        ordered = list(_in_order(function.body))
        for index, (node, _parts) in enumerate(ordered):
            name = _budget_name(node)
            if name is None:
                continue
            for later, parts in ordered[index + 1 :]:
                # A loop is the pass's work: a retry's attempt, a walk of rows.
                if _reads(parts, name) or isinstance(
                    later, ast.For | ast.AsyncFor | ast.While
                ):
                    break
                if isinstance(later, ast.With | ast.AsyncWith):
                    yield function.name, node.lineno
                    break
