"""Find a raw ``threading.Barrier`` in a control test.

A barrier placed in a patched product method waits again when the product
calls that method a second time on the same thread (a replay after a
unique-key conflict, a replan). That thread then waits alone until the timeout
breaks the barrier, and the test fails at random with ``BrokenBarrierError``
instead of naming the re-entry. ``insert_collision_support.Rendezvous`` lets
each thread pass once and fails at once on a second wait.
"""

from __future__ import annotations

import ast

KIND = "raw threading.Barrier in a test; use insert_collision_support.Rendezvous"
SUPPORT = "control/tests/insert_collision_support.py"


def in_scope(path: str) -> bool:
    return (
        path.startswith("control/tests/") and path.endswith(".py") and path != SUPPORT
    )


def scan_source(source: str) -> list[int]:
    tree = ast.parse(source)
    modules = {"threading"}
    barriers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "threading"
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "threading":
            barriers.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "Barrier"
            )
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "Barrier"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in modules
            )
            or (isinstance(node.func, ast.Name) and node.func.id in barriers)
        )
    )
