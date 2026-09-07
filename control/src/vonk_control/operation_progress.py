"""Constant-space progress sampling and advisory projection.

One snapshot per attempt is retained. Rates use adjacent Controller receipt
samples, so reconnects cannot make agent wall-clock jumps look like throughput.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from math import exp

from vonk_agent_protocol import OperationMemberProgress, OperationProgress

TRANSFER_PHASES = frozenset(
    {
        "download",
        "downloading",
        "transfer",
        "transferring",
        "copying",
        "upload",
        "uploading",
        "distribution",
    }
)
WAITING_PHASES = frozenset({"queued", "pending", "waiting", "waiting-for-operator"})
STALE_AFTER_SECONDS = 45.0
STALL_AFTER_SECONDS = 120.0
PROGRESS_INTERVAL_SECONDS = 1.0


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def progress_write_due(
    previous: Mapping[str, object] | None, current: Mapping[str, object], now: datetime
) -> bool:
    """One byte write/second; phase changes have a ten-write/second ceiling."""
    if not previous:
        return True
    observed = _timestamp(previous.get("observed_at"))
    interval = (
        0.1
        if previous.get("phase") != current.get("phase")
        else PROGRESS_INTERVAL_SECONDS
    )
    return observed is None or (now - observed).total_seconds() >= interval


def observe_progress(
    previous: Mapping[str, object] | None, current: Mapping[str, object], now: datetime
) -> dict[str, object]:
    """Attach receipt timing to an already validated monotonic update."""
    old = previous or {}
    result = dict(current)
    prior_time = _timestamp(old.get("observed_at"))
    delta_time = max(0.0, (now - prior_time).total_seconds()) if prior_time else 0.0
    elapsed = float(old.get("elapsed_seconds") or 0.0) + delta_time
    advanced = any(
        int(result.get(key) or 0) > int(old.get(key) or 0)
        for key in ("completed_bytes", "completed_items")
    ) or result["phase"] != old.get("phase")
    result.update(
        observed_at=now.isoformat(),
        elapsed_seconds=elapsed,
        last_progress_at=now.isoformat()
        if advanced
        else old.get("last_progress_at", now.isoformat()),
    )
    # Never carry a transfer estimate into hashing, extraction, or startup.
    for key in ("bytes_per_second", "smoothed_bytes_per_second", "eta_seconds"):
        result.pop(key, None)
    if (
        delta_time > 0
        and result["phase"] in TRANSFER_PHASES
        and result["phase"] == old.get("phase")
    ):
        delta = max(
            0,
            int(result.get("completed_bytes") or 0)
            - int(old.get("completed_bytes") or 0),
        )
        rate = min(10**15, delta / delta_time)
        prior_rate = old.get("smoothed_bytes_per_second")
        # Time-aware exponential smoothing, with a ten-second time constant.
        alpha = 1.0 - exp(-delta_time / 10.0)
        smoothed = (
            rate
            if prior_rate is None
            else float(prior_rate) + alpha * (rate - float(prior_rate))
        )
        result.update(bytes_per_second=rate, smoothed_bytes_per_second=smoothed)
        total = result.get("total_bytes")
        if delta > 0 and total is not None and smoothed > 0:
            result["eta_seconds"] = min(
                10**9,
                max(
                    0.0,
                    (int(total) - int(result.get("completed_bytes") or 0)) / smoothed,
                ),
            )
    return project_progress(OperationProgress.model_validate(result), now).model_dump(
        mode="json", exclude_none=True
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
    activity = "waiting" if stale or value.phase in WAITING_PHASES else "active"
    if (
        value.phase in TRANSFER_PHASES
        and stopped
        and (value.total_bytes is None or value.completed_bytes < value.total_bytes)
    ):
        activity = "possibly_stalled"
    changes: dict[str, object] = {"activity": activity}
    if stale or value.phase not in TRANSFER_PHASES:
        changes.update(
            bytes_per_second=None, smoothed_bytes_per_second=None, eta_seconds=None
        )
    return value.model_copy(update=changes)


def aggregate_progress(members: Sequence[OperationMemberProgress]) -> OperationProgress:
    """Sum parallel members; aggregate ETA is the slowest known completion."""
    known = bool(members) and all(member.total_bytes is not None for member in members)
    active = [
        member
        for member in members
        if member.state not in {"succeeded", "accepted", "compensated"}
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
    phase = next(iter(phases)) if len(phases) == 1 else "transfer" if phases and phases <= TRANSFER_PHASES else "prepare"
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
            member.activity == "waiting" or member.phase in WAITING_PHASES
            for member in active
        )
        else "active",
        members=list(members),
    )
