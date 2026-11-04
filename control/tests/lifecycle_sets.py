"""A set of lifecycle states is named once, in the subject's ``*_states.py``.

"Live" spelled by hand drifts: sites listed ``queued, running, needs-operator``
while the stored rows also carried ``backoff`` and ``observing``, so a load
backing off for its retry was invisible to the newer load that should retire it
(principle 20). Added Controller lines may not build a tuple, set, list or
``words(...)``/``means(...)`` call out of two or more lifecycle states that
includes ``queued`` or ``running`` and no terminal state: that is a partial
"not ended" predicate, and it belongs in the subject's ``*_states.py`` next to
``LIVE``. A full vocabulary (one that names a terminal state too) is a
contract listing, not a predicate, and is not judged here.
"""

from __future__ import annotations

import ast

KIND = (
    "hand-spelled set of lifecycle states; use the subject's named predicate "
    "(job_states.LIVE, fleet_profile_states.LIVE, ...) or name it in its *_states.py"
)
SCOPE = "control/src/vonk_control/"
_ACTIVE = frozenset({"QUEUED", "RUNNING"})
_OPEN = _ACTIVE | {"BACKOFF", "OBSERVING", "NEEDS_OPERATOR"}
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "SUPERSEDED"})
_WORDS = {
    "queued": "QUEUED",
    "running": "RUNNING",
    "backoff": "BACKOFF",
    "observing": "OBSERVING",
    "needs-operator": "NEEDS_OPERATOR",
    "waiting-for-operator": "NEEDS_OPERATOR",
    "succeeded": "SUCCEEDED",
    "failed": "FAILED",
    "cancelled": "CANCELLED",
    "superseded": "SUPERSEDED",
}


def in_scope(path: str) -> bool:
    return (
        path.startswith(SCOPE)
        and path.endswith(".py")
        and not path.endswith("_states.py")
    )


def _lifecycle_owner(node: ast.expr) -> bool:
    """Whether ``node`` names the lifecycle vocabulary (not RunState and kin)."""
    name = ast.unparse(node).split(".")[-1]
    return name.endswith(("LifecycleState", "_states")) or name in {
        "State",
        "aos",
        "ajs",
    }


def _member(node: ast.expr) -> str | None:
    if isinstance(node, ast.Starred):
        return None
    if isinstance(node, ast.Attribute) and node.attr == "value":
        node = node.value
    if isinstance(node, ast.Attribute) and node.attr in _OPEN | _TERMINAL:
        return node.attr if _lifecycle_owner(node.value) else None
    if isinstance(node, ast.Name) and node.id in _OPEN | _TERMINAL:
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _WORDS.get(node.value)
    return None


def scan_source(source: str) -> list[int]:
    """Every line of a construct that spells a partial set of open states."""
    hits: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Tuple, ast.Set, ast.List)):
            items = node.elts
        elif isinstance(node, ast.Call):
            items = node.args
        else:
            continue
        members = {member for item in items if (member := _member(item))}
        if len(members) >= 2 and members & _ACTIVE and not members & _TERMINAL:
            hits.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return sorted(hits)
