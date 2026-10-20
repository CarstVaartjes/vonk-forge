"""Reject lifetime monotonic validators on agent progress, including checkpoints."""

from __future__ import annotations

import ast
from itertools import pairwise

_COUNTERS = frozenset(
    {"completed_bytes", "completed_items", "activity_sequence", "sequence"}
)


def scan_source(source: str) -> list[int]:
    """A counter-to-counter refusal needs the canonical attempt-key boundary.

    Total/size bounds are contract checks, not monotonic comparisons. Rates and
    stall detectors do not refuse evidence, so they are outside this rule.
    """
    hits: list[int] = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        guarded = False
        for statement in function.body:
            if (
                isinstance(statement, ast.If)
                and isinstance(statement.test, ast.UnaryOp)
                and isinstance(statement.test.op, ast.Not)
                and isinstance(statement.test.operand, ast.Call)
                and ast.unparse(statement.test.operand.func) == "same_progress_attempt"
                and any(isinstance(node, ast.Return) for node in statement.body)
            ):
                guarded = True
            if guarded:
                continue
            for branch in ast.walk(statement):
                if not isinstance(branch, ast.If) or not any(
                    isinstance(node, ast.Raise)
                    for child in branch.body
                    for node in ast.walk(child)
                ):
                    continue
                for comparison in ast.walk(branch.test):
                    if not isinstance(comparison, ast.Compare):
                        continue
                    operands = (comparison.left, *comparison.comparators)
                    fields = [
                        {
                            node.attr
                            for node in ast.walk(operand)
                            if isinstance(node, ast.Attribute)
                        }
                        for operand in operands
                    ]
                    if any(
                        left & right & _COUNTERS for left, right in pairwise(fields)
                    ):
                        hits.append(branch.lineno)
                        break
    return sorted(set(hits))
