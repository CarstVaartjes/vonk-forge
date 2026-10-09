"""Stop for fleet qualification campaign cli."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..fleet_qualification import (
    _BudgetClient,
    _durable_key,
    _forget_key,
    _observe,
)
from .contracts import Batch, Campaign, ResultsLog
from .profiles import _apply_profile, _save_profile
from .status import _recipe_summary


def stop(
    client: Any,
    campaign: Campaign,
    log: ResultsLog,
    batch: Batch,
    number: int,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, object]:
    deadline = clock() + campaign.timeout_seconds
    bounded = _BudgetClient(client, deadline, clock)
    _observe(
        lambda: _durable_key(
            log.path.with_suffix(".requests"),
            f"{campaign.authority_id}/{number}/{batch.batch_id}/stop",
        ),
        deadline,
        clock,
        sleeper,
        campaign.poll_seconds,
    )
    _observe(
        lambda: _save_profile(bounded, number, campaign.authority_id, []),
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
        request_scope=f"{batch.batch_id}/stop",
    )
    record = log.append(
        step="stop",
        batch=batch.batch_id,
        status="passed",
        qualified={
            key: _recipe_summary(campaign, log, key)["qualified"]
            for key in batch.recipes
        },
    )
    _forget_key(
        log.path.with_suffix(".requests"),
        f"{campaign.authority_id}/{number}/{batch.batch_id}/stop",
        key,
    )
    return {"step": "stop", "batch": batch.batch_id, "result": record}
