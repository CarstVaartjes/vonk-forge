"""Detect bounded core collection producers without explicit continuation.

This syntax guard covers SQL limits, Python prefix slices and Rust iterator or
Docker listing caps. Behaviour tests still own cursor progress and exact effects.
Byte readers and display/log tails are not core identity collections.
"""

from __future__ import annotations

import ast
import re

KIND = "collection-limit-without-continuation"
CORE = re.compile(
    r"profile|installation|run_scope|container|(?:^|_)jobs?(?:_|$)", re.IGNORECASE
)
CONTINUATION = re.compile(
    r"next_(?:cursor|after|page)|(?:^|_)after\b|\bbefore\b|\bboundary\b|\bcursor\b|rotat\w*|\.offset\("
)


def scan_python(source: str, tree: ast.Module | None = None) -> list[int]:
    tree = tree if tree is not None else ast.parse(source)
    hits = []
    for scope in ast.walk(tree):
        if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        text = ast.unparse(scope)
        collection_producer = (
            re.search(r"list|names|inventory|page", scope.name, re.IGNORECASE)
            and CORE.search(text)
        ) or re.search(
            r"profiles|installations|run_scopes|containers|jobs",
            scope.name + ast.unparse(scope.args),
        )
        if not collection_producer:
            continue
        # Match executable cursor/rotation evidence, never a prose promise.
        if CONTINUATION.search(text):
            continue
        scalar_limits = {
            id(child)
            for call in ast.walk(scope)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr in {"scalar", "exists"}
            for child in ast.walk(call)
        }
        for node in ast.walk(scope):
            if id(node) in scalar_limits:
                continue
            if (
                (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "limit"
                )
                or (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.slice, ast.Slice)
                    and node.slice.lower is None
                    and node.slice.upper is not None
                    and isinstance(node.value, ast.Name | ast.Attribute)
                    and re.search(
                        r"profiles|installations|run_scopes|containers|jobs",
                        ast.unparse(node.value),
                    )
                )
                or (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and re.search(
                        r"\bSELECT\b.*\bFROM\b.*\bLIMIT\b",
                        node.value,
                        re.IGNORECASE | re.DOTALL,
                    )
                    and CORE.search(node.value)
                )
            ):
                hits.append(node.lineno)
    return sorted(set(hits))


def scan_rust(source: str) -> list[int]:
    # Scope at function granularity; comments cannot supply continuation.
    code = re.sub(r"//[^\n]*", "", source)
    hits = []
    for match in re.finditer(r"\bfn\s+(\w+)\s*[^\{]*\{", code):
        start = match.end()
        depth = 1
        end = start
        while end < len(code) and depth:
            depth += (code[end] == "{") - (code[end] == "}")
            end += 1
        body = code[start:end]
        if not (
            CORE.search(match.group(1)) or '"--last"' in body or "MAX_REPORTED_" in body
        ) or CONTINUATION.search(match.group(1) + body):
            continue
        for cap in re.finditer(r'"--last"|\.take\([^\n)]*\)', body):
            line = code.count("\n", 0, start + cap.start()) + 1
            if re.search(r"\.chars\(\)\s*$", body[: cap.start()]):
                continue
            # Read::take bounds bytes, not rows.
            if "read_to_end" not in body and "read_to_string" not in body:
                hits.append(line)
    return sorted(set(hits))


def scan_source(path: str, source: str, tree: ast.Module | None = None) -> list[int]:
    if path.endswith(".py"):
        return scan_python(source, tree)
    if path.endswith(".rs"):
        return scan_rust(source)
    return []


def bounded_rust_page_loop(body: str) -> bool:
    """A finite page traversal advances its page and stops at exhaustion.

    The page producer owns ordering/cursor semantics. This only recognizes the
    fetch-before-removal pattern; a variable named cursor alone is insufficient.
    """
    return bool(
        re.search(r"let next_page\s*=", body)
        and re.search(r"\.list_\w+\(", body)
        and re.search(r"match next_page\??\s*\{", body)
        and re.search(r"(?:Ok\()?Some\(next\)\)?\s*=>\s*(?:page|ids)\s*=\s*next", body)
        and re.search(r"(?:Ok\()?None\)?\s*=>\s*(?:return|break)", body)
    )
