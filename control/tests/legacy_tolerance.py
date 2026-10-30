"""No new tolerance for old or damaged data (P18, one contract, one path).

A reader accepts the current contract. Data written by an older build is
migrated by deploy order, and a damaged row is an unknown the caller re-observes
or refuses, never an alternate shape that parses, or an item that silently
disappears. Added lines in Controller and protocol source may not introduce
compatibility words, alias adopters, an ignored ``retire_as_unknown`` answer or
an ``except`` that turns a malformed value into an absent one. There is no
allowlist: the stored-JSON readers are where new tolerance would land.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize

SCOPE = ("control/src/", "agent_protocol/src/")
# ``compatibility`` alone is a live contract field; only the phrases are not.
WORDS = re.compile(
    r"(?i)\blegacy\b|\blegacy_\w+|\w+_legacy\b|\bbackfill\b|\bshim\b"
    r"|\bbackwards?[ _-]compat\w*|older (agent|controller|build|deploy)"
)
ADOPTERS = re.compile(r"\b(state_adopter|machine_adopter)\(|\bStateAlias\b")
MALFORMED = frozenset(
    {
        "ValidationError",
        "ValueError",
        "TypeError",
        "Exception",
        "BaseException",
        "JSONDecodeError",
    }
)


def in_scope(path: str) -> bool:
    return path.startswith(SCOPE)


def _names(node: ast.expr | None) -> set[str]:
    if node is None:
        return set()
    parts = node.elts if isinstance(node, ast.Tuple) else [node]
    return {
        part.attr if isinstance(part, ast.Attribute) else getattr(part, "id", "")
        for part in parts
    }


def _empty(value: ast.expr | None) -> bool:
    """``None``, ``""``, ``[]``, ``{}``, ``()``, ``set()`` and the like."""
    if value is None:
        return True
    if isinstance(value, ast.Constant):
        return value.value in (None, "", b"")
    if isinstance(value, ast.List | ast.Tuple | ast.Set):
        return not value.elts
    if isinstance(value, ast.Dict):
        return not value.keys
    return (
        isinstance(value, ast.Call)
        and not value.args
        and not value.keywords
        and ast.unparse(value.func) in {"set", "list", "dict", "tuple", "frozenset"}
    )


def _absent(body: list[ast.stmt]) -> bool:
    """The handler ends by dropping the value, whatever it did first.

    Its last statement is ``pass``, ``continue``, ``...``, a return of nothing
    or an empty literal, or an assignment of one.
    """
    last = body[-1]
    if isinstance(last, ast.Pass | ast.Continue):
        return True
    if isinstance(last, ast.Expr):
        return isinstance(last.value, ast.Constant)
    if isinstance(last, ast.Return):
        return _empty(last.value)
    if isinstance(last, ast.Assign | ast.AnnAssign):
        return _empty(last.value)
    return False


def _is_retire(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Call)
        and ast.unparse(node.func).split(".")[-1] == "retire_as_unknown"
    )


def _discarded(node: ast.AST, tree: ast.Module) -> bool:
    if isinstance(node, ast.Expr):
        return _is_retire(node.value)
    if not isinstance(node, ast.Assign | ast.AnnAssign) or not _is_retire(node.value):
        return False
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    read = {
        item.id
        for item in ast.walk(tree)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    }
    return all(
        isinstance(target, ast.Name) and (target.id == "_" or target.id not in read)
        for target in targets
    )


def _code_lines(source: str, tree: ast.Module) -> dict[int, str]:
    """Each line's code: no comments, no docstrings, no ``PRAGMA`` strings."""
    docstrings = {
        (node.body[0].lineno, node.body[0].col_offset)
        for node in ast.walk(tree)
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    text = source.splitlines()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        skipped = (
            token.type == tokenize.COMMENT
            or token.start in docstrings
            or (
                token.type == tokenize.STRING
                and re.match(
                    r"""[rbuf]*['"]+\s*PRAGMA\b""", token.string, re.IGNORECASE
                )
            )
        )
        if not skipped:
            continue
        for row in range(token.start[0], token.end[0] + 1):
            line = text[row - 1]
            start = token.start[1] if row == token.start[0] else 0
            end = token.end[1] if row == token.end[0] else len(line)
            text[row - 1] = line[:start] + " " * (end - start) + line[end:]
    return dict(enumerate(text, 1))


def scan_source(source: str) -> list[tuple[int, str]]:
    """Lines that add tolerance, with what they add."""
    hits: list[tuple[int, str]] = []
    tree = ast.parse(source)
    for line, text in _code_lines(source, tree).items():
        if WORDS.search(text):
            hits.append((line, "legacy or compatibility path"))
        if ADOPTERS.search(text):
            hits.append((line, "state alias or adopter"))
    for node in ast.walk(tree):
        if _discarded(node, tree):
            hits.append(
                (getattr(node, "lineno", 0), "retire_as_unknown answer discarded")
            )
        if (
            isinstance(node, ast.ExceptHandler)
            and (node.type is None or _names(node.type) & MALFORMED)
            and _absent(node.body)
        ):
            hits.append((node.lineno, "malformed value becomes absent"))
        if isinstance(node, ast.With) and any(
            isinstance(item.context_expr, ast.Call)
            and ast.unparse(item.context_expr.func).split(".")[-1] == "suppress"
            and (
                not item.context_expr.args
                or _names(ast.Tuple(item.context_expr.args, ast.Load())) & MALFORMED
            )
            for item in node.items
        ):
            hits.append((node.lineno, "malformed value becomes absent"))
    return hits
