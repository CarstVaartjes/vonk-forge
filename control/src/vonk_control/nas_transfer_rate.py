"""One owner for "this Spark copies from the NAS far slower than the others".

A slow link to the NAS is a wait nobody sees: every model copy to that Spark
simply takes longer.  This module measures it from what the copies did.  A
succeeded model transfer (``artifact.distribution.v1``) keeps the bytes it
reported and the Controller's own receipt time over the attempt, so its rate is
bytes over that elapsed time, over the whole order (it includes finishing
steps), never a guessed link speed.  It is decided at read time from those
rows; nothing is stored or counted.

A transfer can claim bytes it did not move: an object already present, or the
resumed part of an interrupted one, is reported as done before any byte
arrives, so that order looks faster than the link is.  Each Spark is therefore
judged by the median of its recent transfers of at least ``_MIN_BYTES``, and a
Spark is slow only against another Spark's median.  An inflated sample can
hide a slow Spark, never name a healthy one.  One Spark, no comparable
transfers, or an unreadable table shows nothing and never fails the read.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as OperationKind
from vonk_agent_protocol import LifecycleState, stored_words

from .agent_operation_states import ORDER
from .models import AgentOperation, AgentOperationAttempt
from .operation_progress import stored_progress

_LOGGER = logging.getLogger(__name__)
#: The transfers a Spark is judged by: its newest ones, of real model size.
_RECENT = 5
_MIN_BYTES = 1 << 30
_LOOKBACK = timedelta(days=7)
#: Slow means under a quarter of the fastest Spark's median rate.
_SLOW_FRACTION = 0.25


@dataclass(frozen=True, slots=True)
class NasTransferNotice:
    detail: str
    recommendation: str


@dataclass(frozen=True, slots=True)
class _Rate:
    bytes_per_second: float
    transfers: int
    latest_bytes: int
    latest_seconds: float


def _rate(session: Session, node_id: str, since: datetime) -> _Rate | None:
    attempts = session.scalars(
        select(AgentOperationAttempt)
        .join(
            AgentOperation,
            and_(
                AgentOperationAttempt.operation_id == AgentOperation.id,
                AgentOperationAttempt.attempt == AgentOperation.current_attempt,
            ),
        )
        .where(
            AgentOperation.node_id == node_id,
            AgentOperation.kind == OperationKind.ARTIFACT_DISTRIBUTION.value,
            AgentOperation.state.in_(stored_words(ORDER, (LifecycleState.SUCCEEDED,))),
            AgentOperation.created_at >= since,
        )
        .order_by(AgentOperation.created_at.desc())
        .limit(_RECENT * 4)
    )
    samples: list[tuple[int, float]] = []
    for attempt in attempts:
        progress = stored_progress(attempt)
        if progress is None or progress.completed_bytes < _MIN_BYTES:
            continue
        if not progress.elapsed_seconds:
            continue
        samples.append((progress.completed_bytes, progress.elapsed_seconds))
        if len(samples) == _RECENT:
            break
    if not samples:
        return None
    return _Rate(
        bytes_per_second=median(size / seconds for size, seconds in samples),
        transfers=len(samples),
        latest_bytes=samples[0][0],
        latest_seconds=samples[0][1],
    )


def _mb(value: float) -> str:
    return f"{value / 1e6:.1f} MB/s"


def slow_nas_transfers(
    session: Session, names: Mapping[str, str], now: datetime
) -> dict[str, NasTransferNotice]:
    """Each Spark whose recent copies from the NAS are far below the fastest's."""

    try:
        rates = {
            node_id: rate
            for node_id in names
            if (rate := _rate(session, node_id, now - _LOOKBACK)) is not None
        }
    except SQLAlchemyError as error:
        _LOGGER.warning("NAS transfer rates are unavailable: %s", type(error).__name__)
        return {}
    if len(rates) < 2:
        return {}
    fastest = max(rates, key=lambda node_id: rates[node_id].bytes_per_second)
    best = rates[fastest].bytes_per_second
    notices: dict[str, NasTransferNotice] = {}
    for node_id, rate in rates.items():
        if rate.bytes_per_second >= best * _SLOW_FRACTION:
            continue
        plural = "" if rate.transfers == 1 else "s"
        detail = (
            f"Copies from the NAS at {_mb(rate.bytes_per_second)} vs "
            f"{_mb(best)} on {names[fastest]} (median of {rate.transfers} recent "
            f"transfer{plural}; latest {rate.latest_bytes / 1e9:.1f} GB in "
            f"{rate.latest_seconds:.0f} s)."
        )
        notices[node_id] = NasTransferNotice(
            detail[:256],
            f"Check this Spark's network path to the NAS: its model copies take "
            f"about {best / rate.bytes_per_second:.0f}x as long as on "
            f"{names[fastest]}."[:256],
        )
    return notices
