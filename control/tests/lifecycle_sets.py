"""A set of lifecycle states is named once, in the subject's ``*_states.py``.

Sets spelled by hand drift: "live" listed ``queued, running, needs-operator``
while the stored rows also carried ``backoff`` and ``observing``, so a load
backing off for its retry was invisible to the newer load that should retire
it, and one "ended" set counted ``needs-operator`` as ended (principle 20).
Added Controller lines may not build a tuple, set, list or
``words(...)``/``means(...)`` call out of two or more lifecycle states: a
predicate ("live", "ended", "stoppable") belongs in the subject's
``*_states.py``. A listing of every lifecycle state is the vocabulary itself,
and contract modules own the vocabularies they publish; neither is judged.
"""

from __future__ import annotations

import ast

KIND = (
    "hand-spelled set of lifecycle states; use the subject's named predicate "
    "(job_states.LIVE, fleet_profile_states.LIVE, ...) or name it in its *_states.py"
)
SCOPE = "control/src/vonk_control/"
_OPEN = frozenset({"QUEUED", "RUNNING", "BACKOFF", "OBSERVING", "NEEDS_OPERATOR"})
_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "SUPERSEDED"})
_ALL = _OPEN | _TERMINAL
# Only the open words are matched as strings: "failed" or "succeeded" alone
# also belong to installation and run vocabularies.
_WORDS = {
    "queued": "QUEUED",
    "running": "RUNNING",
    "backoff": "BACKOFF",
    "observing": "OBSERVING",
    "needs-operator": "NEEDS_OPERATOR",
    "waiting-for-operator": "NEEDS_OPERATOR",
}


def in_scope(path: str) -> bool:
    return (
        path.startswith(SCOPE)
        and path.endswith(".py")
        and not path.endswith(("_states.py", "_contract.py"))
        and "_contract/" not in path
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
    if isinstance(node, ast.Attribute) and node.attr in _ALL:
        return node.attr if _lifecycle_owner(node.value) else None
    if isinstance(node, ast.Name) and node.id in _ALL:
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _WORDS.get(node.value)
    return None


def scan_source(source: str) -> list[int]:
    """Every line of a construct that spells a set of lifecycle states."""
    hits: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Tuple, ast.Set, ast.List)):
            items = node.elts
        elif isinstance(node, ast.Call):
            items = node.args
        else:
            continue
        members = {member for item in items if (member := _member(item))}
        if len(members) >= 2 and members != _ALL:
            hits.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return sorted(hits)
