"""Stall windows: a wait is not a failure; only a stall ends work.

Work that keeps reporting progress is healthy however long it takes. A window
starts when the work is accepted and restarts at each progress signal; only a
full window without one is a stall. Lease renewal, retries and status writes
are not progress: they prove a worker is alive, not that the work advances.

Work that waits on a live owner (a queue, another load, a Spark that may
return, an image preparation, an eviction in progress) is not stalled at all:
its wait never expires. ``progress_verdict`` is the one decision, and only its
``STALLED`` verdict may end work. Time spent waiting is not charged to the
window: a caller re-anchors its window at every ``WAITING`` verdict
(``waited_anchor``), so a load that waited two hours on another one does not
stall the moment that one finishes. A fixed wall-clock cap survives only as a
transport inactivity bound that retries.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from vonk_agent_protocol import OperationProgress


class Verdict(StrEnum):
    """What a piece of work is doing, as far as ending it is concerned."""

    PROGRESSING = "progressing"
    WAITING = "waiting"
    STALLED = "stalled"


class Liveness(Protocol):
    """An owner as ``reservation_owners.owner`` resolves it: unknown is live."""

    @property
    def live(self) -> bool: ...


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def progress_advanced(
    previous: OperationProgress | None, current: OperationProgress
) -> bool:
    """Whether ``current`` moved past ``previous``: more bytes, items or a new phase."""

    return (
        previous is None
        or current.completed_bytes > previous.completed_bytes
        or (current.completed_items or 0) > (previous.completed_items or 0)
        or current.phase != previous.phase
    )


def last_progress_at(progress: OperationProgress | None) -> datetime | None:
    """When a sampled progress snapshot last advanced, if it says."""

    value = progress.last_progress_at if progress is not None else None
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def latest_progress(
    stored: datetime | None, *signals: datetime | None, now: datetime
) -> datetime | None:
    """The newest progress signal, never later than ``now``.

    A signal from the future (a skewed or damaged clock) cannot hold work open.
    """

    known = [_aware(item) for item in (stored, *signals) if item is not None]
    if not known:
        return None
    return min(max(known), _aware(now))


def stall_deadline(
    started_at: datetime, progressed_at: datetime | None, window: timedelta
) -> datetime:
    """The instant work with no progress since ``progressed_at`` counts as stalled."""

    started = _aware(started_at)
    anchor = started if progressed_at is None else max(started, _aware(progressed_at))
    return anchor + window


def progress_verdict(
    *,
    started_at: datetime,
    progressed_at: datetime | None,
    window: timedelta,
    now: datetime,
    waiting_on: Liveness | Iterable[Liveness] | None = None,
) -> Verdict:
    """``WAITING`` on any live owner, ``STALLED`` after a silent window, else progressing.

    ``progressed_at`` is the newest own-progress signal (or ``waited_anchor``);
    a signal from the future cannot hold work open.
    """

    owners = (
        ()
        if waiting_on is None
        else (waiting_on,)
        if not isinstance(waiting_on, Iterable)
        else tuple(waiting_on)
    )
    if any(owner.live for owner in owners):
        return Verdict.WAITING
    anchor = latest_progress(None, progressed_at, now=now)
    if _aware(now) >= stall_deadline(started_at, anchor, window):
        return Verdict.STALLED
    return Verdict.PROGRESSING


def waited_anchor(
    anchor: datetime | None, verdict: Verdict, now: datetime
) -> datetime | None:
    """The window's anchor after ``verdict``: a wait on a live owner restarts it."""

    return _aware(now) if verdict is Verdict.WAITING else anchor
