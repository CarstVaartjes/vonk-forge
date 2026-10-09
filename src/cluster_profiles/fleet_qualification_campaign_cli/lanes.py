"""Lanes for fleet qualification campaign cli."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..fleet_qualification import (
    QualificationError,
    QualificationObservationUnknown,
)
from .contracts import Batch, Campaign, Lane
from .profiles import _alias, _profile


def _lanes_from_sparks(
    campaign: Campaign, batch: Batch, sparks: Sequence[str]
) -> list[Lane]:
    rows = [campaign.rows[key] for key in batch.recipes]
    needed = sum(row.node_count for row in rows)
    if len(sparks) != needed or len(set(sparks)) != len(sparks):
        raise QualificationError(
            f"{batch.batch_id} needs {needed} distinct --spark IDs in lane order"
        )
    lanes: list[Lane] = []
    offset = 0
    for row in rows:
        alias, kind = _alias(campaign, row)
        nodes = tuple(sparks[offset : offset + row.node_count])
        lanes.append(Lane(row, nodes, alias, kind))
        offset += row.node_count
    return lanes


def _lanes_from_profile(
    client: Any, number: int, campaign: Campaign, batch: Batch
) -> list[Lane]:
    profile = _profile(client, number, campaign.authority_id)
    assigned = {
        item.get("recipe_selector"): tuple(item.get("spark_ids") or ())
        for item in profile.get("assignments") or []
    }
    if set(assigned) != set(batch.recipes):
        raise QualificationObservationUnknown(
            f"profile {number} batch membership is not yet observed"
        )
    lanes = []
    for key in batch.recipes:
        row = campaign.rows[key]
        alias, kind = _alias(campaign, row)
        lanes.append(Lane(row, assigned[key], alias, kind))
    return lanes
