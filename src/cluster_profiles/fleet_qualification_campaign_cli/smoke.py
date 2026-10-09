"""Smoke for fleet qualification campaign cli."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..cli_states import FAILED, SUCCEEDED
from ..control_client import (
    ControlClientError,
)
from ..fleet_qualification import (
    ArtifactJobSmokeAdapter,
    QualificationError,
    QualificationObservationUnknown,
    ServiceSmokeAdapter,
)
from ..qualification_fixtures import FixtureError
from .contracts import Batch, Campaign, Lane, ResultsLog
from .profiles import _wait_serving


def _smoke(
    client: Any,
    lane: Lane,
    run_id: str,
    campaign: Campaign,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    number: int,
    request_directory: Path | None = None,
    request_scope: str = "",
) -> dict[str, object]:
    if lane.kind == "openai-service":
        return ServiceSmokeAdapter(
            campaign.fixtures, timeout_seconds=campaign.timeout_seconds
        ).run(
            client,
            number,
            lane.alias,
            recipe_key=lane.row.key,
            recipe_content_sha256=lane.row.content_sha256,
        )
    return ArtifactJobSmokeAdapter(
        campaign.fixtures,
        request_directory=request_directory,
        request_scope=request_scope,
    ).run(
        client,
        run_id,
        recipe_key=lane.row.key,
        recipe_content_sha256=lane.row.content_sha256,
        interface=lane.row.interface,
        timeout_seconds=campaign.timeout_seconds,
        poll_interval_seconds=campaign.poll_seconds,
        clock=clock,
        sleeper=sleeper,
    )


def _smoke_lanes(
    client: Any,
    lanes: Sequence[Lane],
    campaign: Campaign,
    log: ResultsLog,
    batch: Batch,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    number: int,
    **entry: object,
) -> list[dict[str, object]]:
    """Wait for and smoke every lane concurrently; record one line per lane."""

    def one(lane: Lane) -> dict[str, object]:
        try:
            run_id = _wait_serving(client, lane, campaign, clock, sleeper)
            result = _smoke(
                client,
                lane,
                run_id,
                campaign,
                clock,
                sleeper,
                number,
                log.path.with_suffix(".requests"),
                f"{batch.batch_id}/{entry.get('step')}/{entry.get('failure_mode')}",
            )
        except (
            ControlClientError,
            FixtureError,
            QualificationError,
            QualificationObservationUnknown,
            OSError,
        ) as error:
            return log.append(
                batch=batch.batch_id,
                recipe=lane.row.key,
                status="failed",
                error=str(error)[:1024],
                **entry,
            )
        cases = result.get("cases")
        passed = (
            isinstance(cases, list)
            and bool(cases)
            and all(
                isinstance(case, Mapping) and case.get("state", SUCCEEDED) == SUCCEEDED
                for case in cases
            )
        )
        return log.append(
            batch=batch.batch_id,
            recipe=lane.row.key,
            status="passed" if passed else FAILED,
            run_id=run_id,
            node_ids=list(lane.node_ids),
            result=result,
            **entry,
        )

    with ThreadPoolExecutor(max_workers=max(1, len(lanes))) as executor:
        return list(executor.map(one, lanes))


# Steps ---------------------------------------------------------------------
