"""Status for fleet qualification campaign cli."""

from __future__ import annotations

from typing import Any

from ..fleet_qualification import (
    QualificationError,
)
from .contracts import _PASSED, FAILURE_MODES, Batch, Campaign, ResultsLog, Row


def _modes(row: Row) -> list[str]:
    declared = {ref.failure_mode for ref in row.recovery}
    return [mode for mode in FAILURE_MODES if mode in declared]


def _recipe_summary(campaign: Campaign, log: ResultsLog, key: str) -> dict[str, Any]:
    row = campaign.rows[key]
    recovery = {mode: log.latest("recover", key, mode) for mode in _modes(row)}
    smoke = log.latest("smoke", key)
    return {
        "smoke": smoke,
        "recovery": recovery,
        "qualified": smoke == "passed"
        and all(value in _PASSED for value in recovery.values()),
        # License facts are information for the operator, never a gate.
        "licenses": [
            {"model": item.get("key"), "spdx": item.get("spdx"), "url": item.get("url")}
            for item in row.licenses
        ],
    }


def _next_step(campaign: Campaign, log: ResultsLog, batch: Batch) -> str:
    summaries = [_recipe_summary(campaign, log, key) for key in batch.recipes]
    if any(item["smoke"] == "pending" for item in summaries):
        return "load"
    if any(
        item["smoke"] == "passed"
        and any(value not in _PASSED for value in item["recovery"].values())
        for item in summaries
    ):
        return "recover"
    return "stop"


def _select_batch(
    campaign: Campaign, log: ResultsLog, batch_id: str | None
) -> Batch | None:
    if batch_id is not None:
        for batch in campaign.batches:
            if batch.batch_id == batch_id:
                return batch
        raise QualificationError(f"batch {batch_id} is not in the authority")
    return next(
        (batch for batch in campaign.batches if not log.stopped(batch.batch_id)), None
    )


def status(
    campaign: Campaign, log: ResultsLog, batch: Batch | None
) -> dict[str, object]:
    recipes = {key: _recipe_summary(campaign, log, key) for key in campaign.rows}
    return {
        "status": "complete" if batch is None else "in-progress",
        "authority_id": campaign.authority_id,
        "qualified_recipe_count": sum(
            1 for item in recipes.values() if item["qualified"]
        ),
        "recipe_count": len(recipes),
        "next": None
        if batch is None
        else {
            "batch": batch.batch_id,
            "mode": batch.mode,
            "recipes": list(batch.recipes),
            "step": _next_step(campaign, log, batch),
        },
        "recipes": recipes,
    }


# Controller helpers --------------------------------------------------------
