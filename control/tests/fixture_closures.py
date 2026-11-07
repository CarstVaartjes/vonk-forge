"""Reject a hand-written fixture closure of a serving check.

A serving check stages the package files its request names: a job check names
its fixture and input slots, a multipart check names its audio file. The recipe
contract owns that list as ``RecipeValidationCheck.fixture_paths()``; a closure
written by hand (``request.fixture``, ``request.input_slots``) knows only the
transports it was written for. The Controller's package check once missed
``request.file.fixture`` that way. Ask the check for its paths.
"""

from __future__ import annotations

import ast

KIND = "hand-written fixture closure; use check.fixture_paths()"
_ATTRIBUTES = frozenset({"fixture", "input_slots"})
_REQUESTS = frozenset({"request", "req"})


def _under_request(node: ast.expr, aliases: set[str]) -> bool:
    """Whether an attribute chain passes through a request (or a name bound to one)."""
    while isinstance(node, ast.Attribute):
        if node.attr in _REQUESTS:
            return True
        node = node.value
    return isinstance(node, ast.Name) and (node.id in _REQUESTS or node.id in aliases)


def _request_aliases(tree: ast.AST) -> set[str]:
    """Names bound to a request, e.g. ``job = check.request``."""
    aliases: set[str] = set()
    for _ in range(3):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr):
                value = node.value
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if value is not None and _under_request(value, aliases):
                    aliases.update(t.id for t in targets if isinstance(t, ast.Name))
    return aliases


def scan_source(source: str) -> list[int]:
    tree = ast.parse(source)
    aliases = _request_aliases(tree)
    hits: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            if node.attr == "input_slots" or (
                node.attr == "fixture" and _under_request(node.value, aliases)
            ):
                hits.add(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _ATTRIBUTES
        ):
            hits.add(node.lineno)
    return sorted(hits)
