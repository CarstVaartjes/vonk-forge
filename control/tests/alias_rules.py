"""Find a model alias rule written by hand instead of the one contract type.

A profile accepted 128-character aliases while the run plan, the endpoint
contract, the discovery query and the route candidate each kept their own
63-character copy: the profile saved, then the load could not be planned and
its endpoint never published. ``vonk_control.alias_contract`` owns the rule;
every other alias names ``Alias``, ``ALIAS_PATTERN`` or ``ALIAS_MAX_LENGTH``.

A hit is a length literal (``max_length=63``, ``String(128)``) or a regex
repetition (``{0,62}``) inside a definition whose name says alias.
"""

from __future__ import annotations

import ast
import re

KIND = "hand-written alias length rule; use vonk_control.alias_contract"
OWNER = "control/src/vonk_control/alias_contract.py"
_ALIAS_NAME = re.compile(r"alias", re.IGNORECASE)
_REPETITION = re.compile(r"\{\d*,?\d+\}")
_LENGTH_KEYWORDS = frozenset({"max_length", "length"})


def in_scope(path: str) -> bool:
    return path.startswith("control/src/") and path.endswith(".py") and path != OWNER


def _length_literals(node: ast.AST) -> list[int]:
    hits: list[int] = []
    for child in ast.walk(node):
        if isinstance(child, ast.keyword) and child.arg in _LENGTH_KEYWORDS:
            if isinstance(child.value, ast.Constant) and isinstance(
                child.value.value, int
            ):
                hits.append(child.value.lineno)
        elif (
            isinstance(child, ast.Call)
            and getattr(child.func, "id", getattr(child.func, "attr", "")) == "String"
            and child.args
            and isinstance(child.args[0], ast.Constant)
            and isinstance(child.args[0].value, int)
        ):
            hits.append(child.args[0].lineno)
        elif (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and _REPETITION.search(child.value)
        ):
            hits.append(child.lineno)
    return hits


def _named_alias(target: ast.expr) -> bool:
    name = getattr(target, "id", getattr(target, "attr", ""))
    return bool(_ALIAS_NAME.search(name))


def scan_source(source: str) -> list[int]:
    hits: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign) and _named_alias(node.target):
            hits.update(_length_literals(node))
        elif isinstance(node, ast.Assign) and any(
            _named_alias(target) for target in node.targets
        ):
            hits.update(_length_literals(node.value))
        elif isinstance(node, ast.arguments):
            positional = [*node.posonlyargs, *node.args]
            defaults: dict[int, ast.expr | None] = {
                id(argument): default
                for argument, default in [
                    *zip(
                        positional[len(positional) - len(node.defaults) :],
                        node.defaults,
                    ),
                    *zip(node.kwonlyargs, node.kw_defaults),
                ]
            }
            for argument in [*positional, *node.kwonlyargs]:
                if not _ALIAS_NAME.search(argument.arg):
                    continue
                for part in (argument.annotation, defaults.get(id(argument))):
                    if part is not None:
                        hits.update(_length_literals(part))
    return sorted(hits)
