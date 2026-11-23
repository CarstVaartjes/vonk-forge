"""Endings helpers."""

from __future__ import annotations

from datetime import datetime

from vonk_control.models.jobs import Job
from vonk_control.run_switch_operations.provider import _ADAPTER


def _reject_invalid_operation(job: Job, reason: str, now: datetime) -> None:
    """Reject one malformed persisted operation while retaining its evidence.

    The invalid contract is neither repaired nor replaced: the job keeps its
    stored plan and result bytes, gains a precise operator-visible reason, and
    stops being advanced.  Its already-issued effects still require their own
    cancellation receipts, so this never claims they were stopped.
    """

    _ADAPTER.reject(job, reason, now)
