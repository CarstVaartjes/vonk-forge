"""Find a length term in an ingress path rule that restates a model bound.

``_typed_*_string`` in the protocol contracts only exempts a leaf from the
filesystem-path ban. The leaf's model owns its length: a copy here drifts when
the model changes (``len(value) <= 2048`` kept dropping failure diagnostics
after the log tail became 16384).
"""

from __future__ import annotations

import ast
import re

KIND = "length term in a _typed_*_string path rule; the model owns the bound"
PATH = "agent_protocol/src/vonk_agent_protocol/contracts.py"
RULE = re.compile(r"_typed_\w*string")


def in_scope(path: str) -> bool:
    return path == PATH


def scan_source(source: str) -> list[int]:
    lines: list[int] = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef) or not RULE.fullmatch(
            function.name
        ):
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Call)
                and isinstance(node.left.func, ast.Name)
                and node.left.func.id == "len"
                and [ast.unparse(arg) for arg in node.left.args] == ["value"]
            ):
                lines.append(node.lineno)
    return lines
