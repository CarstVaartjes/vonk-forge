"""No new tolerance for old or damaged data (P18, one contract, one path).

A reader accepts the current contract. Data written by an older build is
migrated by deploy order, and a damaged row is an unknown the caller re-observes
or refuses, never an alternate shape that parses, or an item that silently
disappears. Added lines in Controller and protocol source may not introduce
compatibility words, alias adopters, an ignored ``retire_as_unknown`` answer or
an ``except`` that turns a malformed value into an absent one.
"""

from __future__ import annotations

import ast
import re

SCOPE = ("control/src/", "agent_protocol/src/")
# The modules that own reading stored and strict JSON and the lifecycle evidence
# vocabulary may spell these words and swallow nothing else.
ALLOWED = (
    "control/src/vonk_control/lifecycle/evidence.py",
    "control/src/vonk_control/stored_json.py",
    "control/src/vonk_control/strict_json.py",
)
WORDS = re.compile(
    r"(?i)\b(legacy|compat(ibility)?|backfill|shim)\b"
    r"|older (agent|controller|build|deploy)|adopt_legacy"
)
ADOPTERS = re.compile(r"\b(state_adopter|machine_adopter)\(|\bStateAlias\b")
MALFORMED = frozenset({"ValidationError", "ValueError", "TypeError"})


def in_scope(path: str) -> bool:
    return path.startswith(SCOPE) and path not in ALLOWED


def _names(node: ast.expr | None) -> set[str]:
    if node is None:
        return set()
    parts = node.elts if isinstance(node, ast.Tuple) else [node]
    return {
        part.attr if isinstance(part, ast.Attribute) else getattr(part, "id", "")
        for part in parts
    }


def scan_source(source: str) -> list[tuple[int, str]]:
    """Lines that add tolerance, with what they add."""
    hits: list[tuple[int, str]] = []
    for line, text in enumerate(source.splitlines(), 1):
        if WORDS.search(text):
            hits.append((line, "legacy or compatibility path"))
        if ADOPTERS.search(text):
            hits.append((line, "state alias or adopter"))
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func).split(".")[-1] == "retire_as_unknown"
        ):
            hits.append((node.lineno, "retire_as_unknown answer discarded"))
        if (
            isinstance(node, ast.ExceptHandler)
            and _names(node.type) & MALFORMED
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Continue | ast.Pass)
        ):
            hits.append((node.lineno, "malformed value becomes absent"))
    return hits
