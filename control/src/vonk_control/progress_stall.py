"""Stuck work is seen: a wait with nothing moving for a stall window is an alarm.

A profile load that waits is never ended for waiting: a wait is not a failure.
But a load that records the same wait with nothing moving anywhere (no step, no
phase, no bytes, no preparation progressing for it, no different reason) for a
whole stall window is work nobody is moving, and it must be visible long before
someone finds it by hand.

A stall is judged by time without movement, never by how often the wait was
retried or re-checked: a preparation, a build or an inventory that takes long
is retried many times while it works, and a retry is not evidence of anything.

Every write of an application's progress while it is queued, running or
backing off and waits on something (it records blockers) is observed: a
backoff whose every retry is declined the same way is a wait that does not
move, and a newer load waiting for an older one to finish the assignments it
continues records that wait too.  Its *signature* is the
load's own movement (state, step, child operation, child phase, bytes, items,
the child's last-progress time, admission, and when a preparation it asked for
last progressed) together with what it waits on: each blocker's code and its
Sparks.  A changing symptom is not movement: the blocker text (with the times
and counters it quotes) and the phase of a re-check the child runs are outside
the signature; a phase the child's operation really advances shows in its
last-progress time.  Retry bookkeeping (attempts, due times, status text) is
outside the signature too.
Any change of the signature starts the stall over, and a load that stops
waiting or ends drops it.  ``repeats`` counts the retry attempts that saw the
same wait; it is informative and decides nothing.

The stall is stored with the progress (``stall``).  Whether it is an alarm is
decided when it is read, against :data:`PROGRESS_STALL_WINDOW` since the wait
last moved: nothing is cancelled or paused, and the alarm clears with the first
movement.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TypeGuard

from pydantic import ValidationError
from sqlalchemy import event, inspect
from vonk_agent_protocol import LifecycleState, canonical_message
from vonk_agent_protocol.agent_words import ProfileReasonSeverity

from .fleet_profile_contract import FleetProfileApplicationProgress
from .models import FleetProfileApplication
from .operation_blockers import (
    OperationBlocker,
    ProgressAlarmCode,
    ProgressStall,
    make_blocker,
)
from .settings import PROFILE_STALL_WINDOW_SECONDS
from .stall_window import Verdict, progress_verdict

#: How long a wait shows no movement before it is shown as stalled: the load's
#: own stall window.  It is longer than a preparation's own stall window, so a
#: preparation that stops working ends (and changes the wait) before its load
#: is ever called stalled for it.
PROGRESS_STALL_WINDOW = timedelta(seconds=PROFILE_STALL_WINDOW_SECONDS)

#: The states in which a load's wait is observed and can be shown stalled.
WAITING_STATES = frozenset(
    {
        LifecycleState.QUEUED.value,
        LifecycleState.RUNNING.value,
        LifecycleState.BACKOFF.value,
    }
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _retry_attempt(
    progress: FleetProfileApplicationProgress,
) -> tuple[int, int, int | None]:
    """Where the retries stand: one advances once per retry, never per check."""

    child = progress.child_progress
    return (
        progress.attempt,
        progress.admission_attempt,
        None if child is None else child.retry_attempt,
    )


def _waits(progress: FleetProfileApplicationProgress) -> list[OperationBlocker]:
    return [
        item for item in progress.blockers if item.code != ProgressAlarmCode.STALLED
    ]


def progress_signature(
    progress: FleetProfileApplicationProgress,
    *,
    state: str,
    current_step: int | None,
    current_operation_id: str | None,
) -> str:
    """One digest of a load's movement and of what it waits on."""

    child = progress.child_progress
    operation = child.operation if child is not None else None
    waits = sorted((item.code, sorted(item.node_ids)) for item in _waits(progress))
    movement = [
        state,
        current_step,
        current_operation_id,
        progress.completed_steps,
        progress.admission_pending,
        # A preparation working for the wait: its advancement is movement.
        progress.preparation_progressed_at,
        None if child is None else child.phase,
        None if child is None else child.bytes,
        None if operation is None else operation.completed_bytes,
        None if operation is None else operation.completed_items,
        None if operation is None else operation.last_progress_at,
    ]
    canonical = json.dumps([movement, waits], sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def read_stall(value: object) -> ProgressStall | None:
    """A stored stall, or ``None`` when absent or unreadable (never fatal)."""

    if not isinstance(value, dict):
        return None
    try:
        return ProgressStall.model_validate_json(canonical_message(value), strict=True)
    except (TypeError, ValueError):
        return None


def next_stall(
    previous: ProgressStall | None,
    progress: FleetProfileApplicationProgress,
    *,
    before: FleetProfileApplicationProgress | None,
    state: str,
    current_step: int | None,
    current_operation_id: str | None,
    observed_at: datetime,
) -> ProgressStall | None:
    """The stall after one more observation of ``progress``, or ``None``.

    ``before`` is the progress this observation replaces; a repeat counts only
    when the retry attempt moved since it (``None``: unknown, not counted).
    """

    waits = _waits(progress)
    if state not in WAITING_STATES or not waits:
        return None
    now = _aware(observed_at)
    signature = progress_signature(
        progress,
        state=state,
        current_step=current_step,
        current_operation_id=current_operation_id,
    )
    if previous is not None and previous.signature == signature:
        if before is None or _retry_attempt(before) == _retry_attempt(progress):
            return previous  # checked again within the same attempt
        if now <= _aware(previous.last_seen_at):
            return previous  # the same observation written twice
        return previous.model_copy(
            update={"last_seen_at": now, "repeats": previous.repeats + 1}
        )
    return ProgressStall(
        signature=signature,
        detail=f"{waits[0].code}: {waits[0].detail}"[:512],
        first_seen_at=now,
        last_seen_at=now,
        repeats=1,
    )


def _read_progress(value: object) -> FleetProfileApplicationProgress | None:
    if not isinstance(value, dict):
        return None
    try:
        return FleetProfileApplicationProgress.model_validate_json(
            canonical_message(value), strict=True
        )
    except (TypeError, ValueError, ValidationError):
        return None


def observe_application_progress(
    _mapper: object, _connection: object, target: FleetProfileApplication
) -> None:
    """Flush hook: count each written progress of a profile application.

    Every writer replaces ``progress`` as a whole, so the flush is the one place
    that sees each observation, whichever path recorded the wait.  A document
    that does not read is left as it is: its own readers own that residue.
    """

    history = inspect(target).attrs.progress.history
    current = target.progress
    if not history.has_changes() or not isinstance(current, dict):
        return
    progress = _read_progress(current)
    if progress is None:
        return
    old = history.deleted[0] if history.deleted else current
    previous = read_stall(old.get("stall")) if isinstance(old, dict) else None
    observed_at = target.updated_at or target.created_at
    stall = (
        None
        if observed_at is None
        else next_stall(
            previous,
            progress,
            before=_read_progress(old),
            state=str(target.state),
            current_step=target.current_step,
            current_operation_id=target.current_operation_id,
            observed_at=observed_at,
        )
    )
    stored = None if stall is None else stall.model_dump(mode="json")
    if current.get("stall") != stored:
        document = {key: value for key, value in current.items() if key != "stall"}
        if stored is not None:
            document["stall"] = stored
        target.progress = document


# Registered with the module: every process that reads or writes profile loads
# imports this one (the application projection and the platform summary do).
event.listen(FleetProfileApplication, "before_insert", observe_application_progress)
event.listen(FleetProfileApplication, "before_update", observe_application_progress)


def is_stalled(
    stall: ProgressStall | None, *, now: datetime
) -> TypeGuard[ProgressStall]:
    """Whether the wait has not moved for a whole :data:`PROGRESS_STALL_WINDOW`."""

    return (
        stall is not None
        and progress_verdict(
            started_at=stall.first_seen_at,
            progressed_at=None,
            window=PROGRESS_STALL_WINDOW,
            now=now,
        )
        is Verdict.STALLED
    )


def stalled_blocker(stall: ProgressStall) -> OperationBlocker:
    """The alarm a stalled load shows next to the wait it keeps repeating."""

    return make_blocker(
        ProgressAlarmCode.STALLED,
        f"No progress since {_aware(stall.first_seen_at).isoformat()}: {stall.detail}",
        severity=ProfileReasonSeverity.ERROR.value,
    )


__all__ = [
    "PROGRESS_STALL_WINDOW",
    "WAITING_STATES",
    "is_stalled",
    "next_stall",
    "observe_application_progress",
    "progress_signature",
    "read_stall",
    "stalled_blocker",
]
