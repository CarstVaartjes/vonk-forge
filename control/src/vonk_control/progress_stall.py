"""Stuck work is seen: a wait with nothing moving for a stall window is an alarm.

A profile load that waits is never ended for waiting: a wait is not a failure.
But a load that records the same wait with nothing moving anywhere (no step, no
phase, no bytes, no preparation progressing for it, no different reason) for a
whole stall window is work nobody is moving, and it must be visible long before
someone finds it by hand.

A stall is judged by time without movement, never by how often the wait was
retried or re-checked: a preparation, a build or an inventory that takes long
is retried many times while it works, and a retry is not evidence of anything.

Every write of an application's progress or of its persisted wait while it is
queued, running or backing off and waits on something (blockers in its progress
or in its persisted wait, such as a missing child receipt) is observed: a
backoff whose every retry is declined the same way is a wait that does not
move, and a newer load waiting for an older one to finish the assignments it
continues records that wait too.  A load holds no lease, so every running
load carries such a wait (at least its child's missing receipt): only forward
movement keeps a working load out of the alarm.

The stall's *signature* is where the load waits: its state, step, child
operation, completed steps and admission.  A different place starts a new
stall.  At one place only forward movement counts, never a change, because a
stuck child cycles through its symptoms (2026-10-24: receipt-missing,
run-switch.phase-retry, a runtime preflight, receipt-missing again, each
retry copying from zero).  The stall keeps what the wait has reached:

* every wait seen (code and Sparks): one not seen yet is movement, one seen
  before is not;
* per child phase seen (``marks``): the child's own transfer bytes, the
  operation's bytes, items and subprocess activity at their highest, and the
  operation's own phases; a phase not seen yet or a counter past its mark is
  movement.  The operation's counters count within one checkpoint key: a new
  key under the child attempt that first showed the old key re-executes it
  (an agent restart) and its counters start over, while a new key under a
  later attempt is a retry and must pass the old marks;
* the latest progress of a preparation working for the wait.

So a copy retried from zero that rises again below its old mark, a phase
flapping back to one it already showed, a wait recurring, or a fresh
last-progress time alone never restarts the window.  The blocker text (with
the times and counters it quotes) and retry bookkeeping (attempts, due times,
status text) are outside the stall.  A load that stops waiting or ends drops
it.  ``repeats`` counts the retry attempts that saw the same place; it is
informative and decides nothing.  What is kept is bounded by the closed child
phase and reason vocabularies, the Sparks and the agent's own phase names:
seeing the same again never grows it.

The stall is stored with the progress (``stall``).  Whether it is an alarm is
decided when it is read, against :data:`PROGRESS_STALL_WINDOW` since the wait
last moved: nothing is cancelled or paused, and the alarm clears with the first
movement.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
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
    StallMark,
    StallWait,
    make_blocker,
)
from .settings import PROFILE_STALL_WINDOW_SECONDS
from .stall_window import Verdict, progress_verdict
from .wait_contract import WaitEvidence

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


def _waits(
    progress: FleetProfileApplicationProgress, waiting: Sequence[OperationBlocker]
) -> list[OperationBlocker]:
    """What the load waits on: its progress blockers, then its persisted wait's."""

    causes = [
        item for item in progress.blockers if item.code != ProgressAlarmCode.STALLED
    ]
    seen = {(item.code, tuple(sorted(item.node_ids))) for item in causes}
    return [
        *causes,
        *(
            item
            for item in waiting
            if item.code != ProgressAlarmCode.STALLED
            and (item.code, tuple(sorted(item.node_ids))) not in seen
        ),
    ]


def progress_signature(
    progress: FleetProfileApplicationProgress,
    *,
    state: str,
    current_step: int | None,
    current_operation_id: str | None,
) -> str:
    """One digest of where the load waits: its place, never what it shows."""

    place = [
        state,
        current_step,
        current_operation_id,
        progress.completed_steps,
        progress.admission_pending,
    ]
    canonical = json.dumps(place, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _advance(
    previous: Sequence[StallMark], progress: FleetProfileApplicationProgress
) -> tuple[list[StallMark], bool]:
    """The marks raised by the child's current phase, and whether that moved.

    Moving is reaching what this wait has not reached before: a child phase or
    an operation phase not seen yet, or a counter past its mark.  The
    operation's counters count within one checkpoint key: a new key under the
    attempt that first showed the old one re-executes it and starts its
    counters over; a new key under a later attempt is a retry and must pass
    the old marks.  An unkeyed sample keeps the key it does not name.
    """

    child = progress.child_progress
    if child is None:
        return list(previous), False
    operation = child.operation
    marks = {mark.phase: mark for mark in previous}
    old = marks.get(child.phase)
    base = old or StallMark(phase=child.phase)
    key = (
        None
        if operation is None or operation.checkpoint is None
        else operation.checkpoint.key
    )
    if key is not None and key != base.checkpoint_key:
        if base.checkpoint_key is not None and base.key_attempt == child.retry_attempt:
            base = base.model_copy(
                update={"operation_bytes": 0, "operation_items": 0, "activity": 0}
            )
        base = base.model_copy(
            update={"checkpoint_key": key, "key_attempt": child.retry_attempt}
        )
    reached = {
        "child_bytes": child.bytes or 0,
        "operation_bytes": 0 if operation is None else operation.completed_bytes,
        "operation_items": 0 if operation is None else operation.completed_items or 0,
        "activity": 0 if operation is None else operation.activity_sequence or 0,
    }
    raised = {
        name: value for name, value in reached.items() if value > getattr(base, name)
    }
    phases = list(base.operation_phases)
    if operation is not None and operation.phase not in phases:
        phases.append(operation.phase)
    marks[child.phase] = base.model_copy(update={**raised, "operation_phases": phases})
    moved = old is None or bool(raised) or len(phases) > len(base.operation_phases)
    return list(marks.values()), moved


def _seen_waits(
    previous: Sequence[StallWait], waits: Sequence[OperationBlocker]
) -> tuple[list[StallWait], bool]:
    """The waits seen at this place, and whether one of ``waits`` is new."""

    seen = list(previous)
    keys = {(item.code, tuple(item.node_ids)) for item in seen}
    for item in waits:
        nodes = sorted(item.node_ids)
        if (item.code, tuple(nodes)) not in keys:
            keys.add((item.code, tuple(nodes)))
            seen.append(StallWait(code=item.code, node_ids=nodes))
    return seen, len(seen) > len(previous)


def _latest(first: datetime | None, second: datetime | None) -> datetime | None:
    known = [_aware(item) for item in (first, second) if item is not None]
    return max(known) if known else None


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
    waiting: Sequence[OperationBlocker] = (),
) -> ProgressStall | None:
    """The stall after one more observation of ``progress``, or ``None``.

    ``before`` is the progress this observation replaces; a repeat counts only
    when the retry attempt moved since it (``None``: unknown, not counted).
    ``waiting`` is the load's persisted wait: a wait recorded only there (a
    missing child receipt, an unconfirmed stop) is a wait like any other.
    """

    waits = _waits(progress, waiting)
    if state not in WAITING_STATES or not waits:
        return None
    seen_at = _aware(observed_at)
    signature = progress_signature(
        progress,
        state=state,
        current_step=current_step,
        current_operation_id=current_operation_id,
    )
    detail = f"{waits[0].code}: {waits[0].detail}"[:512]
    if previous is None or previous.signature != signature:
        marks, _ = _advance((), progress)
        seen, _ = _seen_waits((), waits)
        return ProgressStall(
            signature=signature,
            detail=detail,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            repeats=1,
            marks=marks,
            waits=seen,
            preparation_progressed_at=_latest(None, progress.preparation_progressed_at),
        )
    marks, advanced = _advance(previous.marks, progress)
    seen, new_wait = _seen_waits(previous.waits, waits)
    prepared = _latest(
        previous.preparation_progressed_at, progress.preparation_progressed_at
    )
    if (
        advanced
        or new_wait
        or prepared != _latest(None, previous.preparation_progressed_at)
    ):
        # Forward movement at the same place: the window starts over, and what
        # it reached is kept, so falling back and rising again is not.
        return previous.model_copy(
            update={
                "detail": detail,
                "first_seen_at": seen_at,
                "last_seen_at": seen_at,
                "repeats": 1,
                "marks": marks,
                "waits": seen,
                "preparation_progressed_at": prepared,
            }
        )
    kept = previous.model_copy(update={"marks": marks, "waits": seen})
    if before is None or _retry_attempt(before) == _retry_attempt(progress):
        return kept  # checked again within the same attempt
    if seen_at <= _aware(previous.last_seen_at):
        return kept  # the same observation written twice
    return kept.model_copy(
        update={"last_seen_at": seen_at, "repeats": previous.repeats + 1}
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

    attrs = inspect(target).attrs
    history = attrs.progress.history
    current = target.progress
    # Each write of the progress, the persisted wait or its next check is an
    # observation (a re-check rewrites an equal wait and moves only its due).
    if not any(
        item.history.has_changes()
        for item in (attrs.progress, attrs.wait_evidence, attrs.next_action_at)
    ) or not isinstance(current, dict):
        return
    progress = _read_progress(current)
    if progress is None:
        return
    # Progress producers may replace their typed blockers after the lifecycle
    # transition. Publish those exact causes with the same SQL update and clock.
    if target.next_action_at is not None and progress.blockers:
        from vonk_agent_protocol import WaitReason

        from .persisted_waits import schedule_wait, typed_wait_reason

        causes = [
            b for b in progress.blockers if b.code is not ProgressAlarmCode.STALLED
        ]
        if causes:
            schedule_wait(
                target,
                target.next_action_at,
                now=target.updated_at or target.created_at,
                reason=typed_wait_reason(
                    causes[0].code, WaitReason.OBSERVATION_UNAVAILABLE
                ),
                blockers=causes,
            )
    old = history.deleted[0] if history.deleted else current
    previous = read_stall(old.get("stall")) if isinstance(old, dict) else None
    wait = (
        target.wait_evidence if isinstance(target.wait_evidence, WaitEvidence) else None
    )
    observed_at = target.updated_at or target.created_at
    if observed_at is not None and wait is not None:
        # A wait written without touching the row's time began when it says.
        observed_at = max(_aware(observed_at), _aware(wait.first_blocked_at))
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
            waiting=() if wait is None else wait.blockers,
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
