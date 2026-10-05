"""The state of a composite subject is ``aggregate(children)``.

A Run/Switch operation is a composite over its current child (a recipe job and its
Spark orders); a profile application is a composite over its Run/Switch
operations.  A composite has no effect of its own, so it never carries a retry,
a lease or a cancel of its own: the children's rows are decided by the core, and
the parent *follows* them.  This is the one pure function that says how.

Worst outcome wins once every child has ended (failed, then cancelled, then
succeeded).  While any child has not, the parent is as active as its most active
child: running beats queued beats observing beats backoff; a child that waits for
an operator keeps the parent waiting only when nothing else is moving.
"""

from __future__ import annotations

from collections.abc import Sequence

from .types import TERMINAL_STATES, Lifecycle, State

_ACTIVITY = (State.RUNNING, State.QUEUED, State.OBSERVING, State.BACKOFF)


def aggregate(children: Sequence[Lifecycle]) -> State | None:
    """The parent's state from its children, or ``None`` when it has none."""

    if not children:
        return None
    states = {child.state for child in children}
    if states <= TERMINAL_STATES:
        if State.FAILED in states:
            return State.FAILED
        if State.CANCELLED in states:
            return State.CANCELLED
        return State.SUCCEEDED
    live = states - TERMINAL_STATES
    for state in _ACTIVITY:
        if state in live:
            return state
    return State.NEEDS_OPERATOR


__all__ = ["aggregate"]
