"""Find temporary files whose name another writer can share.

A ``.tmp`` or ``.part`` file named by a literal, ``with_name`` or
``with_suffix`` is the same path for every process and thread that writes
it: one writer's rename or cleanup consumes the other's file. The API and the
worker shared ``snapshot.candidate.json`` and ``.recipe-library.tar.part``
that way, and the catalog sync failed on every retry (#2019). Name the file
with ``tempfile.mkstemp`` (or a ``uuid4`` part) so each writer owns its own.
"""

from __future__ import annotations

import ast
import re

KIND = "fixed .tmp/.part name; let tempfile.mkstemp or uuid4 make it private"
# Calls whose literals only shape a name the call makes unique.
PRIVATE_CALLS = frozenset({"mkstemp", "mkdtemp", "NamedTemporaryFile"})
SCRATCH = re.compile(r"\.(?:tmp|part)$")
# A bare ".tmp" names a file only as a suffix; alone it is a directory name.
NAMED = re.compile(r"[^/]\.(?:tmp|part)$")
SUFFIX_CALLS = frozenset({"with_suffix", "with_name"})
# A resumable ingress partial is deliberately one path per digest: the claim
# on that digest is its single writer, and a restart resumes the bytes.
VERDICTS: dict[str, str] = {
    "control/src/vonk_control/model_cache/storage.py": (
        "resumable per-digest ingress partial; the digest claim is its one writer"
    ),
}


def _name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        # A formatted part stands in as "{}": it names the file's stem.
        return "".join(
            part.value
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
            else "{}"
            for part in node.values
        )
    return None


def scan_source(source: str, path: str = "") -> list[int]:
    if path in VERDICTS:
        return []
    tree = ast.parse(source)
    private: set[int] = set()
    suffixes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node) in PRIVATE_CALLS:
            private.update(id(child) for child in ast.walk(node))
        elif isinstance(node, ast.Call) and _name(node) in SUFFIX_CALLS:
            suffixes.update(id(argument) for argument in node.args)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            suffixes.add(id(node.right))
    lines = source.splitlines()
    hits: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.expr) or id(node) in private:
            continue
        text = _literal(node)
        if text is None or "*" in text or not SCRATCH.search(text):
            continue
        if id(node) not in suffixes and not NAMED.search(text):
            continue
        line = node.lineno
        if re.search(r"\b(?:mkstemp|uuid4)\b", lines[line - 1]):
            continue
        hits.add(line)
    return sorted(hits)
