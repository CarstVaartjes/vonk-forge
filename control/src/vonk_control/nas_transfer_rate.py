"""One owner for "this Spark copies from the NAS far slower than the others".

A slow link to the NAS is a wait nobody sees: every model copy to that Spark
simply takes longer.  This module measures it from what the copies did.  A
succeeded model transfer (``artifact.distribution.v1``) keeps what its agent
measured: the bytes it received over the network and the time between its
first and last received byte.  Its rate is those bytes over that time, never a
guessed link speed.  Objects already present and the resumed part of an
interrupted transfer are completed bytes, not fetched bytes, so a transfer that
fetched nothing is no sample.  It is decided at read time from those rows;
nothing is stored or counted.

Each Spark is judged by the median of its recent samples of at least ``_MIN_BYTES``, and a
Spark is slow only against another Spark's median.  One Spark, no comparable
transfers, or an unreadable table shows nothing and never fails the read.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from sqlalchemy import and_, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from vonk_agent_protocol.agent_operations import AgentOperation as OperationKind
from vonk_agent_protocol.inventory import NetworkInterface
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, stored_words
from vonk_control.agent_operation_states import ORDER
from vonk_control.catalog.revision_collection import GRACE
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt
from vonk_control.operations.progress import stored_progress

_LOGGER = logging.getLogger(__name__)
#: The transfers a Spark is judged by: its newest ones, of real model size.
_RECENT = 5
_MIN_BYTES = 1 << 30
#: The history retention window: older transfers are collected anyway.
_LOOKBACK = GRACE
#: Slow means under a quarter of the fastest Spark's median rate.
_SLOW_FRACTION = 0.25


@dataclass(frozen=True, slots=True)
class NasTransferNotice:
    detail: str
    recommendation: str
    compact_detail: str


@dataclass(frozen=True, slots=True)
class _Rate:
    bytes_per_second: float
    transfers: int
    latest_bytes: int
    latest_seconds: float


def nas_rates(
    session: Session, node_ids: Collection[str], since: datetime
) -> dict[str, _Rate]:
    """Each Spark's rate over its newest transfers that really fetched bytes."""

    fetched = AgentOperationAttempt.progress["fetched"]
    ranked = (
        select(
            AgentOperationAttempt,
            func.row_number()
            .over(
                partition_by=AgentOperation.node_id,
                order_by=AgentOperation.created_at.desc(),
            )
            .label("position"),
        )
        .join(
            AgentOperation,
            and_(
                AgentOperationAttempt.operation_id == AgentOperation.id,
                AgentOperationAttempt.attempt == AgentOperation.current_attempt,
            ),
        )
        .where(
            AgentOperation.node_id.in_(node_ids),
            AgentOperation.kind == OperationKind.ARTIFACT_DISTRIBUTION.value,
            AgentOperation.state.in_(stored_words(ORDER, (LifecycleState.SUCCEEDED,))),
            AgentOperation.created_at >= since,
            fetched["bytes"].as_float() >= _MIN_BYTES,
            fetched["seconds"].as_float() > 0,
        )
        .subquery()
    )
    attempts = aliased(AgentOperationAttempt, ranked)
    rows = session.execute(
        select(AgentOperation.node_id, attempts)
        .join(AgentOperation, attempts.operation_id == AgentOperation.id)
        .where(ranked.c.position <= _RECENT)
        .order_by(AgentOperation.node_id, ranked.c.position)
    )
    samples: dict[str, list[tuple[int, float]]] = {}
    for node_id, attempt in rows:
        progress = stored_progress(attempt)
        if progress is not None and progress.fetched is not None:
            samples.setdefault(node_id, []).append(
                (progress.fetched.bytes, progress.fetched.seconds)
            )
    return {
        node_id: _Rate(
            bytes_per_second=median(size / seconds for size, seconds in found),
            transfers=len(found),
            latest_bytes=found[0][0],
            latest_seconds=found[0][1],
        )
        for node_id, found in samples.items()
    }


def _mb(value: float) -> str:
    return f"{value / 1e6:.1f} MB/s"


def slow_nas_transfers(
    session: Session,
    names: Mapping[str, str],
    now: datetime,
) -> dict[str, NasTransferNotice]:
    """Each Spark whose recent copies from the NAS are far below the fastest's."""

    try:
        rates = nas_rates(session, names, now - _LOOKBACK)
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
            f"Copies from the NAS at {_mb(rate.bytes_per_second)} vs {_mb(best)} "
            f"(median of {rate.transfers} recent transfer{plural})",
        )
    return notices


def wifi_link_detail(
    interfaces: list[NetworkInterface] | None, route: str | None
) -> str:
    """Observed PHY evidence for the NAS route, never a throughput estimate."""
    link = next((item for item in interfaces or [] if item.name == route), None)
    if link is None or link.kind != "wifi" or not link.carrier:
        return ""
    parts: list[str] = []
    if link.signal_dbm is not None:
        parts.append(f"signal {link.signal_dbm} dBm")
    if link.frequency_mhz is not None:
        frequency = link.frequency_mhz
        band = (
            "2.4 GHz"
            if 2400 <= frequency < 2500
            else "5 GHz"
            if 4900 <= frequency < 5925
            else "6 GHz"
            if 5925 <= frequency <= 7125
            else f"{frequency} MHz"
        )
        parts.append(f"on {band}")
    for label, rate in (("TX", link.tx_bitrate_mbps), ("RX", link.rx_bitrate_mbps)):
        if rate is not None:
            parts.append(f"{label} {rate:g} Mbit/s")
    return "; Wi-Fi " + ", ".join(parts) if parts else ""


def nas_transfer_detail(
    notice: NasTransferNotice,
    interfaces: list[NetworkInterface] | None,
    route: str | None,
) -> str:
    link = wifi_link_detail(interfaces, route)
    if not link:
        return notice.detail
    detail = notice.detail.rstrip(".") + link + "."
    if len(detail) > 256:
        # Keep the measured comparison and link evidence within Text256.
        # The recommendation still names the faster peer when its long display
        # name leaves no room for the link measurements in the detail.
        detail = notice.compact_detail + link + "."
    return detail
