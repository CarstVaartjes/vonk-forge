"""Find free text written as a lifecycle reason.

A status or reason stored with lifecycle state is a contract enum value
(``WaitReason``, ``FailureCode``, ``SupersedeCode``, ``RunDegradedReason``, ...),
never prose: prose cannot be decided on, and it drifts from the vocabulary every
client renders.  A string literal, f-string, concatenation or ``.format`` is
free text when it is

* passed as ``reason=``, ``status_reason=``, ``terminal_reason=`` or
  ``retry_reason=``;
* the reason argument of a lifecycle event (``Observed``, ``CancelRequested``,
  ``LeaseLapsed``, ``OperatorAction``, ``RecordResidue``);
* assigned to a ``status_reason`` attribute or to a name such as ``reason``,
  ``why`` or ``*_reason``, or returned by a function named ``*_reason``;
* the fallback (``reason or "text"``) or a branch of any of those.

Rows written before keep their text and still read.  Count-free: the caller
reports only added lines.
"""

from __future__ import annotations

import ast
import re

KEYWORDS = frozenset({"reason", "status_reason", "terminal_reason", "retry_reason"})
#: The positional index of the reason of each lifecycle event.
EVENT_REASON_INDEX = {
    "CancelRequested": 1,
    "LeaseLapsed": 0,
    "Observed": 1,
    "OperatorAction": 1,
    "RecordResidue": 0,
}
_REASON_NAME = re.compile(r"^(?:why|(?:\w*_)?reason)$", re.IGNORECASE)


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def free_text(node: ast.expr | None) -> ast.expr | None:
    """The node that spells prose in ``node``, or ``None`` when it is typed."""

    match node:
        case ast.Constant(value=str() as value) if value.strip():
            return node
        case ast.JoinedStr():
            return node
        case ast.BinOp(left=left, right=right):
            return free_text(left) or free_text(right)
        case ast.BoolOp(values=values):
            return next((hit for value in values if (hit := free_text(value))), None)
        case ast.IfExp(body=body, orelse=orelse):
            return free_text(body) or free_text(orelse)
        case ast.Subscript(value=value):
            return free_text(value)
        case ast.Call(func=ast.Attribute(attr="format", value=value)):
            return free_text(value)
    return None


def _enum_bodies(tree: ast.Module) -> set[int]:
    """Lines of an enum class body: its members are the allowed spelling."""

    return {
        line
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(_name(base).endswith("Enum") for base in node.bases)
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }


def scan_source(source: str, *, path: str = "") -> list[int]:
    """Lines where free text becomes a lifecycle reason."""

    del path
    tree = ast.parse(source)
    enums = _enum_bodies(tree)
    values: list[ast.expr | None] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            values.extend(
                keyword.value for keyword in node.keywords if keyword.arg in KEYWORDS
            )
            index = EVENT_REASON_INDEX.get(_name(node.func))
            if index is not None and len(node.args) > index:
                values.append(node.args[index])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_REASON_NAME.match(_name(target)) for target in targets):
                values.append(node.value)
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and _REASON_NAME.match(node.name.lstrip("_")):
            values.extend(
                statement.value
                for statement in ast.walk(node)
                if isinstance(statement, ast.Return)
            )
    lines = {hit.lineno for value in values if (hit := free_text(value)) is not None}
    return sorted(lines - enums)
