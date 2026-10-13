"""Stall windows: a deadline that only ends work which stopped progressing.

Work that keeps reporting progress is healthy however long it takes. A window
starts when the work is accepted and restarts at each progress signal; only a
full window without one is a stall. Lease renewal, retries and status writes
are not progress: they prove a worker is alive, not that the work advances.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from vonk_agent_protocol import OperationProgress


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
