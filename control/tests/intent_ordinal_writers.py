"""Find code that numbers a workload intent outside ``workload_intent``.

``vonk_control.workload_intent`` is the one writer of
``agent_nodes.workload_intent_ordinal``: a conditional UPDATE that a newer
intent cannot be overwritten by. Reading the Spark rows under FOR UPDATE to
compute ``max(...) + 1`` and assign it was the pattern that serialized every
intent behind agent traffic. This scan flags that arithmetic on an ordinal and
every other write of it (an attribute assignment on anything but a named contract
object, ``setattr``, a raw UPDATE of ``agent_nodes``) anywhere else.
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


# Contract objects that carry an ordinal they were handed; they are not a Spark row.
CONTRACT_RECEIVERS = ("progress", "payload", "parent", "intent")


def _contract_receiver(value: ast.AST) -> bool:
    return isinstance(value, ast.Name) and any(
        value.id == word or value.id.endswith("_" + word) for word in CONTRACT_RECEIVERS
    )


def _ordinal_write(target: ast.AST) -> bool:
    """An attribute write of the ordinal on anything but a named contract object."""
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_ordinal_write(item) for item in target.elts)
    return (
        isinstance(target, ast.Attribute)
        and target.attr == ATTRIBUTE
        and not _contract_receiver(target.value)
    )


def _raw_ordinal_update(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    text = node.value.lower()
    return "update" in text and "agent_nodes" in text and ATTRIBUTE in text


def _setattr_ordinal(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == ATTRIBUTE
    )


def _written_targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For, ast.comprehension)):
        return [node.target]
    return []


def _numbered_from_max(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Add)
        and any(
            _is_max(side) and _mentions_ordinal(side)
            for side in (node.left, node.right)
        )
    )


def scan_source(source: str) -> list[int]:
    """Lines that number an intent, or write a Spark row's ordinal, by any route."""
    hits: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.BinOp, ast.Call, ast.Constant)) and (
            _numbered_from_max(node)
            or _setattr_ordinal(node)
            or _raw_ordinal_update(node)
        ):
            hits.add(node.lineno)
        hits.update(
            target.lineno for target in _written_targets(node) if _ordinal_write(target)
        )
    return sorted(hits)
