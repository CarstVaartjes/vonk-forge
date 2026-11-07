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


def _is_request(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _REQUESTS
    return isinstance(node, ast.Attribute) and node.attr in _REQUESTS


def scan_source(source: str) -> list[int]:
    hits: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            if node.attr == "input_slots" or (
                node.attr == "fixture" and _is_request(node.value)
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
