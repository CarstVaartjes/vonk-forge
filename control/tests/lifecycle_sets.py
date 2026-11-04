"""A set of lifecycle states is named once, in the subject's ``*_states.py``.

Sets spelled by hand drift: "live" listed ``queued, running, needs-operator``
while the stored rows also carried ``backoff`` and ``observing``, so a load
backing off for its retry was invisible to the newer load that should retire
it, and one "ended" set counted ``needs-operator`` as ended (principle 20).

An added Controller line may not spell a set of two or more lifecycle states,
"live" or "ended", in any of these shapes:

* a tuple, set or list literal, or the positional arguments of a call
  (``words(...)``, ``means(...)``, ``frozenset(...)``);
* a composition of those: ``(*words(A), *words(B))``, ``words(A) + words(B)``;
* an ``==``/``!=`` chain on one subject: ``s == "queued" or s == "running"``.

A state is a member named on a lifecycle owner (``LifecycleState``, ``State``,
a ``*_states`` module, ``aos``/``ajs``: a name rule, so another machine's
``RunState.RUNNING`` is not one), a bare ``QUEUED``-style name, or a stored
word. ``"failed"`` counts only next to another lifecycle word, because
installations and runs fail too. Not judged: a listing of every lifecycle
state (the vocabulary itself), ``*_states.py``, and contract modules, which
own the vocabularies they publish. Only added lines are checked.
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
#: "failed" alone is also an installation's and a run's word: it counts only
#: in a construct that names another lifecycle state.
_WEAK_FAILED = "failed"
_WORDS = {
    "queued": "QUEUED",
    "running": "RUNNING",
    "backoff": "BACKOFF",
    "observing": "OBSERVING",
    "needs-operator": "NEEDS_OPERATOR",
    "waiting-for-operator": "NEEDS_OPERATOR",
    "succeeded": "SUCCEEDED",
    "cancelled": "CANCELLED",
    "superseded": "SUPERSEDED",
    _WEAK_FAILED: _WEAK_FAILED,
}
_COMBINE = (ast.Add, ast.BitOr)


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


def _composed(node: ast.expr) -> set[str]:
    """The states a set expression names, through ``*`` and ``+``/``|``."""
    if isinstance(node, ast.Starred):
        return _composed(node.value)
    if isinstance(node, (ast.Tuple, ast.Set, ast.List)):
        return {state for item in node.elts for state in _item(item)}
    if isinstance(node, ast.Call):
        return {state for item in node.args for state in _item(item)}
    if isinstance(node, ast.BinOp) and isinstance(node.op, _COMBINE):
        return _composed(node.left) | _composed(node.right)
    return _item(node)


def _item(node: ast.expr) -> set[str]:
    """One element: a state, or a set spliced in with ``*``."""
    if isinstance(node, ast.Starred):
        return _composed(node.value)
    member = _member(node)
    return {member} if member else set()


def _chain(node: ast.BoolOp) -> set[str]:
    """The states an ``==``/``!=`` chain compares one subject with (the most)."""
    by_subject: dict[str, set[str]] = {}
    for value in node.values:
        if (
            isinstance(value, ast.Compare)
            and len(value.ops) == 1
            and isinstance(value.ops[0], (ast.Eq, ast.NotEq))
        ):
            left, right = value.left, value.comparators[0]
            for subject, word in ((left, right), (right, left)):
                if member := _member(word):
                    by_subject.setdefault(ast.unparse(subject), set()).add(member)
    return max(by_subject.values(), key=len, default=set())


def _states(members: set[str]) -> set[str]:
    strong = members - {_WEAK_FAILED}
    if _WEAK_FAILED in members and strong:
        strong.add("FAILED")
    return strong


def scan_source(source: str) -> list[int]:
    """Every line of a construct that spells a set of lifecycle states."""
    hits: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Tuple, ast.Set, ast.List, ast.Call, ast.BinOp)):
            members = _composed(node)
        elif isinstance(node, ast.BoolOp):
            members = _chain(node)
        else:
            continue
        members = _states(members)
        if len(members) >= 2 and members != _ALL:
            hits.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return sorted(hits)
