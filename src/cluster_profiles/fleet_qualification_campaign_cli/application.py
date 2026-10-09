"""Load for fleet qualification campaign cli."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ..cli_states import RUNNING
from ..fleet_qualification import (
    QualificationObservationUnknown,
    _BudgetClient,
    _durable_key,
    _forget_key,
    _observe,
)
from .contracts import Batch, Campaign, ResultsLog
from .lanes import _lanes_from_sparks
from .profiles import _apply_profile, _quote, _save_profile
from .smoke import _smoke_lanes


def load(
    client: Any,
    campaign: Campaign,
    log: ResultsLog,
    batch: Batch,
    number: int,
    sparks: Sequence[str],
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, object]:
    lanes = _lanes_from_sparks(campaign, batch, sparks)
    deadline = clock() + campaign.timeout_seconds
    bounded = _BudgetClient(client, deadline, clock)
    for lane in lanes:

        def content(lane=lane):
            detail = bounded.request("GET", f"/api/recipe/{_quote(lane.row.key)}")
            current = (detail.get("identity") or {}).get("content_sha256")
            if current != lane.row.content_sha256:
                raise QualificationObservationUnknown(
                    "campaign content is not yet observed"
                )
            return detail

        _observe(content, deadline, clock, sleeper, campaign.poll_seconds)
    # Identity exists before the first profile mutation, including lost PUT replies.
    _observe(
        lambda: _durable_key(
            log.path.with_suffix(".requests"),
            f"{campaign.authority_id}/{number}/{batch.batch_id}/load",
        ),
        deadline,
        clock,
        sleeper,
        campaign.poll_seconds,
    )
    _observe(
        lambda: _save_profile(
            bounded,
            number,
            campaign.authority_id,
            [
                {
                    "recipe_selector": lane.row.key,
                    "spark_ids": sorted(lane.node_ids),
                    "assignment_name": lane.alias,
                    "desired_state": RUNNING,
                }
                for lane in lanes
            ],
        ),
        deadline,
        clock,
        sleeper,
        campaign.poll_seconds,
    )
    key = _apply_profile(
        client,
        number,
        campaign,
        clock,
        sleeper,
        request_directory=log.path.with_suffix(".requests"),
        request_scope=f"{batch.batch_id}/load",
    )
    results = _smoke_lanes(
        client, lanes, campaign, log, batch, clock, sleeper, number, step="smoke"
    )
    _forget_key(
        log.path.with_suffix(".requests"),
        f"{campaign.authority_id}/{number}/{batch.batch_id}/load",
        key,
    )
    return {"step": "load", "batch": batch.batch_id, "results": results}
