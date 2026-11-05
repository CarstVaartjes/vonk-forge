"""Find code that numbers a workload intent outside ``workload_intent``.

``vonk_control.workload_intent`` is the one writer of
``agent_nodes.workload_intent_ordinal``: a conditional UPDATE that a newer
intent cannot be overwritten by. Reading the Spark rows under FOR UPDATE to
compute ``max(...) + 1`` and assign it was the pattern that serialized every
intent behind agent traffic. This scan flags that arithmetic on an ordinal and
an assignment to a Spark row's ordinal anywhere else.
"""

from __future__ import annotations

import ast

KIND = (
    "workload intent numbered outside workload_intent; "
    "use allocate_workload_intent / take_workload_intent"
)
OWNER = "control/src/vonk_control/workload_intent.py"
ATTRIBUTE = "workload_intent_ordinal"


def in_scope(path: str) -> bool:
    return path.startswith("control/src/") and path.endswith(".py") and path != OWNER


def _mentions_ordinal(node: ast.AST) -> bool:
    return any(
        (isinstance(item, ast.Attribute) and item.attr == ATTRIBUTE)
        or (isinstance(item, ast.Name) and item.id == ATTRIBUTE)
        for item in ast.walk(node)
    )


def _is_max(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "max"
    )


def _spark_row_ordinal(target: ast.AST) -> bool:
    return (
        isinstance(target, ast.Attribute)
        and target.attr == ATTRIBUTE
        and isinstance(target.value, ast.Name)
        and target.value.id.endswith("node")
    )


def scan_source(source: str) -> list[int]:
    """Lines that add one to a max() of ordinals, or assign a Spark row's ordinal."""
    hits: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
            and any(
                _is_max(side) and _mentions_ordinal(side)
                for side in (node.left, node.right)
            )
        ):
            hits.add(node.lineno)
        if isinstance(node, ast.Assign):
            written: list[ast.AST] = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            written = [node.target]
        else:
            continue
        if any(_spark_row_ordinal(target) for target in written):
            hits.add(node.lineno)
    return sorted(hits)
