"""Constant-space progress sampling and advisory projection.

One snapshot per attempt is retained. Rates use adjacent Controller receipt
samples, so reconnects cannot make agent wall-clock jumps look like throughput.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from math import exp
from typing import Any

from vonk_agent_protocol import (
    TRANSFER_PHASES,
    WAITING_PHASES,
    LifecycleState,
    OperationMemberProgress,
    OperationProgress,
    ProgressPhase,
    adopt_progress_phase,
    canonical_message,
)

from . import agent_operation_states, job_states
from .lifecycle.evidence import Residue
from .stored_json import read_row_column
from .strict_json import read_stored_model

#: Lifecycle words a stored phase can also be while the work waits for something
#: other than bytes; the progress words that wait are ``WAITING_PHASES``.
_LIFECYCLE_WAITING_WORDS = frozenset(
    {
        LifecycleState.OBSERVING.value,
        LifecycleState.BACKOFF.value,
        *job_states.words(LifecycleState.NEEDS_OPERATOR),
    }
)


def is_transfer_phase(word: str) -> bool:
    """Whether a phase word (an agent's, current or retired) is one where bytes move."""
    return adopt_progress_phase(word) in TRANSFER_PHASES


def is_waiting_phase(word: str) -> bool:
    """Whether a phase word means the work is not running yet."""
    return (
        adopt_progress_phase(word) in WAITING_PHASES or word in _LIFECYCLE_WAITING_WORDS
    )


STALE_AFTER_SECONDS = 45.0
STALL_AFTER_SECONDS = 120.0
PROGRESS_INTERVAL_SECONDS = 1.0


def progress_document(progress: OperationProgress) -> Any:
    """The JSON document a progress column stores for ``progress``."""

    return json.loads(canonical_message(progress))


def stored_progress(row: object) -> OperationProgress | None:
    """The progress a row's ``progress`` column holds; a damaged one reads as none.

    Progress is a derived measurement, never evidence: a document that cannot be
    read restarts from the next sample instead of stopping the work.
    """

    value = read_row_column(row, "progress")
    return None if value is None or isinstance(value, Residue) else value


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def progress_write_due(
    previous: OperationProgress | None, current: OperationProgress, now: datetime
) -> bool:
    """One byte write/second; phase changes have a ten-write/second ceiling."""
    if previous is None:
        return True
    observed = _timestamp(previous.observed_at)
    interval = 0.1 if previous.phase != current.phase else PROGRESS_INTERVAL_SECONDS
    return observed is None or (now - observed).total_seconds() >= interval


def sample_progress(
    previous: OperationProgress | None, current: OperationProgress, now: datetime
) -> OperationProgress:
    """Attach receipt timing to an already validated monotonic update."""
    prior_time = _timestamp(previous.observed_at) if previous else None
    delta_time = max(0.0, (now - prior_time).total_seconds()) if prior_time else 0.0
    elapsed = (previous.elapsed_seconds or 0.0 if previous else 0.0) + delta_time
    advanced = (
        previous is None
        or current.completed_bytes > previous.completed_bytes
        or (current.completed_items or 0) > (previous.completed_items or 0)
        or current.phase != previous.phase
    )
    stamp = now.isoformat()
    changes = {
        "observed_at": stamp,
        "elapsed_seconds": elapsed,
        "last_progress_at": stamp
        if advanced
        else (previous.last_progress_at if previous else None) or stamp,
        # Never carry a transfer estimate into hashing, extraction, or startup.
        "bytes_per_second": None,
        "smoothed_bytes_per_second": None,
        "eta_seconds": None,
    }
    if (
        previous is not None
        and delta_time > 0
        and is_transfer_phase(current.phase)
        and current.phase == previous.phase
    ):
        delta = max(0, current.completed_bytes - previous.completed_bytes)
        rate = min(10**15, delta / delta_time)
        prior_rate = previous.smoothed_bytes_per_second
        # Time-aware exponential smoothing, with a ten-second time constant.
        alpha = 1.0 - exp(-delta_time / 10.0)
        smoothed = (
            rate
            if prior_rate is None
            else float(prior_rate) + alpha * (rate - float(prior_rate))
        )
        changes.update(bytes_per_second=rate, smoothed_bytes_per_second=smoothed)
        if delta > 0 and current.total_bytes is not None and smoothed > 0:
            changes["eta_seconds"] = min(
                10**9,
                max(0.0, (current.total_bytes - current.completed_bytes) / smoothed),
            )
    return project_progress(
        read_stored_model(
            OperationProgress, {**current.model_dump(mode="json"), **changes}
        ),
        now,
    )


def project_progress(
    value: OperationProgress, now: datetime | None = None
) -> OperationProgress:
    """Age the snapshot without writing history or changing execution state."""
    now = now or datetime.now(UTC)
    observed = _timestamp(value.observed_at)
    advanced = _timestamp(value.last_progress_at)
    stale = observed is None or (now - observed).total_seconds() >= STALE_AFTER_SECONDS
    stopped = (
        advanced is not None and (now - advanced).total_seconds() >= STALL_AFTER_SECONDS
    )
    activity = "waiting" if stale or is_waiting_phase(value.phase) else "active"
    # A transfer phase is stall-able whenever bytes remain (or are unknown).  A
    # non-transfer phase is stall-able only when the operation itself declared a
    # measurable total that is still incomplete, so a phase with no declared
    # bound (an unbounded build, for example) is never called unhealthy, and an
    # intentional operator wait never is.
    bytes_remaining = (
        value.total_bytes is None or value.completed_bytes < value.total_bytes
    )
    declared_remaining = (
        value.total_bytes is not None and value.completed_bytes < value.total_bytes
    ) or (
        value.total_items is not None
        and (value.completed_items or 0) < value.total_items
    )
    if (
        stopped
        and not is_waiting_phase(value.phase)
        and (
            (is_transfer_phase(value.phase) and bytes_remaining)
            or (not is_transfer_phase(value.phase) and declared_remaining)
        )
    ):
        activity = "possibly_stalled"
    projected = value.model_copy(update={"activity": activity})
    if stale or not is_transfer_phase(value.phase):
        projected = projected.model_copy(
            update={
                "bytes_per_second": None,
                "smoothed_bytes_per_second": None,
                "eta_seconds": None,
            }
        )
    return projected


def project_progress_for_state(
    value: OperationProgress, state: object, now: datetime | None = None
) -> OperationProgress:
    """One advisory freshness policy for every retained attempt consumer."""
    projected = project_progress(value, now)
    if state in {
        LifecycleState.SUCCEEDED,
        "accepted",
        agent_operation_states.RETAINED_COMPENSATED,
        LifecycleState.FAILED,
        LifecycleState.CANCELLED,
        *agent_operation_states.PARKED,
    }:
        return projected.model_copy(
            update={
                "activity": None,
                "bytes_per_second": None,
                "smoothed_bytes_per_second": None,
                "eta_seconds": None,
            }
        )
    return projected


def member_progress(
    value: OperationProgress | None,
    *,
    member_id: str,
    state: str,
    phase: str,
    kind: str | None = None,
) -> OperationMemberProgress:
    """Carry measured fields once; an unissued member has no sample or estimate."""
    if value is None:
        return OperationMemberProgress(
            member_id=member_id,
            state=state,
            phase=phase,
            kind=kind,
            activity="waiting" if state == LifecycleState.QUEUED else None,
        )
    return OperationMemberProgress(
        member_id=member_id,
        state=state,
        phase=value.phase,
        kind=kind or value.kind,
        object_sha256=value.object_sha256,
        completed_bytes=value.completed_bytes,
        total_bytes=value.total_bytes,
        bytes_per_second=value.bytes_per_second,
        smoothed_bytes_per_second=value.smoothed_bytes_per_second,
        eta_seconds=value.eta_seconds,
        completed_items=value.completed_items,
        total_items=value.total_items,
        elapsed_seconds=value.elapsed_seconds,
        observed_at=value.observed_at,
        last_progress_at=value.last_progress_at,
        activity=value.activity,
    )


def aggregate_progress(members: Sequence[OperationMemberProgress]) -> OperationProgress:
    """Sum parallel members; aggregate ETA is the slowest known completion."""
    known = bool(members) and all(member.total_bytes is not None for member in members)
    active = [
        member
        for member in members
        if member.state
        not in {"succeeded", "accepted", agent_operation_states.RETAINED_COMPENSATED}
    ]
    rates_known = bool(active) and all(
        member.bytes_per_second is not None for member in active
    )
    smoothed_known = bool(active) and all(
        member.smoothed_bytes_per_second is not None for member in active
    )
    eta_known = (
        known
        and bool(active)
        and all(member.eta_seconds is not None for member in active)
    )
    phases = {member.phase for member in active or members}
    phase = (
        next(iter(phases))
        if len(phases) == 1
        else ProgressPhase.TRANSFER.value
        if phases and all(is_transfer_phase(word) for word in phases)
        else ProgressPhase.PREPARING.value
    )
    timestamps = [
        member.last_progress_at for member in members if member.last_progress_at
    ]
    observed = [member.observed_at for member in members if member.observed_at]
    return OperationProgress(
        phase=phase,
        completed_bytes=sum(member.completed_bytes for member in members),
        total_bytes=sum(member.total_bytes or 0 for member in members)
        if known
        else None,
        total_bytes_known=known,
        bytes_per_second=sum(member.bytes_per_second or 0 for member in active)
        if rates_known
        else None,
        smoothed_bytes_per_second=sum(
            member.smoothed_bytes_per_second or 0 for member in active
        )
        if smoothed_known
        else None,
        eta_seconds=max(member.eta_seconds or 0 for member in active)
        if eta_known
        else None,
        last_progress_at=max(timestamps) if timestamps else None,
        observed_at=min(observed)
        if len(observed) == len(members) and observed
        else None,
        elapsed_seconds=max((member.elapsed_seconds or 0) for member in members)
        if members and all(member.elapsed_seconds is not None for member in members)
        else None,
        completed_items=sum(member.completed_items or 0 for member in members)
        if members and all(member.completed_items is not None for member in members)
        else None,
        total_items=sum(member.total_items or 0 for member in members)
        if members and all(member.total_items is not None for member in members)
        else None,
        activity="possibly_stalled"
        if any(member.activity == "possibly_stalled" for member in active)
        else "waiting"
        if any(
            member.activity == "waiting" or is_waiting_phase(member.phase)
            for member in active
        )
        else "active",
        members=list(members),
    )
