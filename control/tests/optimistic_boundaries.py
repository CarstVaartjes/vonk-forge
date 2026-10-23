"""Versioned Job and RecipeRun writes must retain their fence; claims alone may lock.

Report lock syntax and owner references so a new chained lock or changed target
cannot hide behind an unchanged opening line. Editing an existing predicate is
not a new lock.
"""

from __future__ import annotations

import ast

KIND = "versioned owner lock or bulk write; use an optimistic decision or SKIP LOCKED claim"


def scan_source(
    source: str, versioned: frozenset[str] = frozenset({"Job", "RecipeRun"})
) -> list[int]:
    tree = ast.parse(source)
    aliases = {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    def model(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and aliases.get(node.id, node.id) in versioned

    def references(node: ast.AST) -> bool:
        return any(model(child) for child in ast.walk(node))

    found: list[int] = []

    def report(node: ast.Call) -> None:
        # Attribute.lineno starts at the receiver's opening line. Its end marks
        # the actual method, after any unchanged select/where chain.
        start = node.func.end_lineno or node.lineno
        found.extend(range(start, (node.end_lineno or start) + 1))
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            name = (
                call.func.id
                if isinstance(call.func, ast.Name)
                else (call.func.attr if isinstance(call.func, ast.Attribute) else "")
            )
            if aliases.get(name, name) in {"select", "query", "AdmissionRowLock"}:
                found.extend(
                    child.lineno
                    for arg in call.args
                    for child in ast.walk(arg)
                    if isinstance(child, ast.Name) and model(child)
                )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "with_for_update":
            if any(
                keyword.arg == "skip_locked"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            ):
                continue
            targets = [
                keyword.value for keyword in node.keywords if keyword.arg == "of"
            ]
            if any(references(target) for target in targets or [node.func.value]):
                report(node)
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {
            "get",
            "get_one",
        }:
            lock = next(
                (kw.value for kw in node.keywords if kw.arg == "with_for_update"), None
            )
            if lock is not None and node.args and model(node.args[0]):
                if isinstance(lock, ast.Dict) and any(
                    isinstance(key, ast.Constant)
                    and key.value == "skip_locked"
                    and isinstance(value, ast.Constant)
                    and value.value is True
                    for key, value in zip(lock.keys, lock.values, strict=True)
                ):
                    continue
                if not (isinstance(lock, ast.Constant) and lock.value is False):
                    report(node)
        elif (
            isinstance(node.func, ast.Name)
            and aliases.get(node.func.id, node.func.id) == "AdmissionRowLock"
        ):
            if len(node.args) > 1 and model(node.args[1]):
                report(node)
        elif (
            isinstance(node.func, ast.Name)
            and aliases.get(node.func.id, node.func.id) == "update"
        ):
            if node.args and model(node.args[0]):
                report(node)
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "update":
            if references(node.func.value):
                report(node)
    return found
