"""Endings helpers."""

from __future__ import annotations

from datetime import datetime

from ..models import (
    Job,
)
from .provider import _ADAPTER


def _reject_invalid_operation(job: Job, reason: str, now: datetime) -> None:
    """Reject one malformed persisted operation while retaining its evidence.

    The invalid contract is neither repaired nor replaced: the job keeps its
    stored plan and result bytes, gains a precise operator-visible reason, and
    stops being advanced.  Its already-issued effects still require their own
    cancellation receipts, so this never claims they were stopped.
    """

    _ADAPTER.reject(job, reason, now)
