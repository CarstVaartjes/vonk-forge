"""Recovery for fleet qualification campaign cli."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..fleet_qualification import (
    QualificationError,
    _BudgetClient,
    _observe,
)
from .contracts import _PASSED, FAILURE_MODES, Batch, Campaign, Lane, ResultsLog
from .lanes import _lanes_from_profile
from .profiles import _fleet_nodes
from .smoke import _smoke_lanes
from .status import _modes


def _boot_id(node: Mapping[str, Any]) -> str | None:
    sample = (node.get("telemetry") or {}).get("sample") or {}
    value = sample.get("boot_id")
    return value if isinstance(value, str) and value else None


def _online(node: Mapping[str, Any]) -> bool:
    return (node.get("connection") or {}).get("online_state") == "online"


def _wait_disruption(
    client: Any,
    baseline: Mapping[str, str | None],
    campaign: Campaign,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> None:
    """Wait until every baseline Spark rebooted or was seen offline."""

    targets = set(baseline)
    disrupted: set[str] = set()
    deadline = clock() + campaign.timeout_seconds
    bounded = _BudgetClient(client, deadline, clock)
    while clock() < deadline:
        nodes = _observe(
            lambda: _fleet_nodes(bounded),
            deadline,
            clock,
            sleeper,
            campaign.poll_seconds,
        )
        for node_id in targets:
            node = nodes.get(node_id) or {}
            boot_id, before = _boot_id(node), baseline.get(node_id)
            if not _online(node) or (boot_id and before and boot_id != before):
                disrupted.add(node_id)
        if disrupted >= targets:
            return
        if clock() >= deadline:
            missing = sorted(targets - disrupted)
            raise QualificationError(f"no restart or outage of {missing} was observed")
        sleeper(min(campaign.poll_seconds, max(0, deadline - clock())))
    raise QualificationError("qualification disruption observation deadline elapsed")


def _recovery_targets(
    mode: str, lanes: Sequence[Lane], failure_spark: str | None
) -> list[str]:
    if mode != "dual-rank-loss-recovery":
        return sorted({node_id for lane in lanes for node_id in lane.node_ids})
    (lane,) = lanes
    if failure_spark is None:
        return [lane.node_ids[-1]]
    if failure_spark not in lane.node_ids:
        raise QualificationError("--failure-spark is not one of the lane's Sparks")
    return [failure_spark]


def recover(
    client: Any,
    campaign: Campaign,
    log: ResultsLog,
    batch: Batch,
    number: int,
    failure_spark: str | None,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    notify: Callable[[dict[str, object]], None],
) -> dict[str, object]:
    lanes = _lanes_from_profile(client, number, campaign, batch)
    results: list[dict[str, object]] = []
    for mode in FAILURE_MODES:
        pending: list[Lane] = []
        for lane in lanes:
            if (
                mode not in _modes(lane.row)
                or log.latest("smoke", lane.row.key) != "passed"
                or log.latest("recover", lane.row.key, mode) in _PASSED
            ):
                continue
            ref = next(ref for ref in lane.row.recovery if ref.failure_mode == mode)
            if (
                ref.role == "shared-member"
                and log.latest("recover", ref.representative, mode) == "passed"
            ):
                results.append(
                    log.append(
                        step="recover",
                        failure_mode=mode,
                        batch=batch.batch_id,
                        recipe=lane.row.key,
                        status="covered",
                        covered_by=ref.representative,
                    )
                )
                continue
            pending.append(lane)
        if not pending:
            continue
        targets = _recovery_targets(mode, pending, failure_spark)
        nodes = _fleet_nodes(client)
        baseline = {node_id: _boot_id(nodes.get(node_id) or {}) for node_id in targets}
        notify(
            {
                "checkpoint": mode,
                "batch": batch.batch_id,
                "recipes": [lane.row.key for lane in pending],
                "action": mode,
                "sparks": targets,
            }
        )
        _wait_disruption(client, baseline, campaign, clock, sleeper)
        results.extend(
            _smoke_lanes(
                client,
                pending,
                campaign,
                log,
                batch,
                clock,
                sleeper,
                number,
                step="recover",
                failure_mode=mode,
            )
        )
    return {"step": "recover", "batch": batch.batch_id, "results": results}
