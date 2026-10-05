"""The shared lifecycle core of the Controller.

Composition, not inheritance: one pure transition function
(:func:`transition`), one adapter protocol per kind (:class:`KindAdapter`), and
one reconcile loop (:class:`Reconciler`).  The design and the migration order are
in the blocker audit (section 5); ``tools/lifecycle-writers-allowlist.json``
lists the lifecycle writers that have not moved onto the core yet.
"""

from .adapter import Dispatch, KindAdapter
from .core import (
    OBSERVE_BUDGET,
    STOP_BUDGET,
    blind_retry_is_safe,
    never_executed,
    transition,
)
from .reconciler import LifecycleStore, Reconciler, ReconcileReport
from .types import (
    TERMINAL_STATES,
    CancelRequested,
    Claimed,
    Command,
    Decision,
    Effect,
    Event,
    Execute,
    Heartbeat,
    LeaseLapsed,
    Lifecycle,
    Observe,
    Observed,
    OperatorAction,
    Outcome,
    RecordResidue,
    Reported,
    State,
    Stop,
    StopResult,
    Submitted,
    Tick,
)

__all__ = [
    "OBSERVE_BUDGET",
    "STOP_BUDGET",
    "TERMINAL_STATES",
    "CancelRequested",
    "Claimed",
    "Command",
    "Decision",
    "Dispatch",
    "Effect",
    "Event",
    "Execute",
    "Heartbeat",
    "KindAdapter",
    "LeaseLapsed",
    "Lifecycle",
    "LifecycleStore",
    "Observe",
    "Observed",
    "OperatorAction",
    "Outcome",
    "ReconcileReport",
    "Reconciler",
    "RecordResidue",
    "Reported",
    "State",
    "Stop",
    "StopResult",
    "Submitted",
    "Tick",
    "blind_retry_is_safe",
    "never_executed",
    "transition",
]
