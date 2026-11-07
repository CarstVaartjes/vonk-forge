"""Distributed recovery: end the runs no live work owns.

A run in ``planned``, ``starting`` or ``stopping`` is waiting on an operation
(a Start, a Stop). Other paths end that operation without an agent result (a
supersession or a Controller cancel before any Spark claimed it), and the
result projection that would have moved the run never runs. The run is then
found by its state: past ``OWNER_SETTLE_GRACE`` with no live work naming it, it
leaves through one edge.

- Every Start the run ever had ended with all its orders unissued
  (``current_attempt == 0``): nothing was launched, so the run is ``stopped``
  and its claims are released, with no Spark round trip.
- Otherwise, or when that cannot be proven (the jobs were pruned), the exact
  Stop is queued (``RunEndings.end_unfinished_run_in_session``).

The move is a compare-and-set on ``RecipeRun.version`` (the mapper's version
column, inside the optimistic publication transaction): a recovery restart or a
new Stop bumps it, so the reaper never undoes a run that was just taken over.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import RunState

from ..logging import log_event
from ..models import IN_FLIGHT_RUN_STATES, AgentOperation, Job, RecipeRun, RunNode
from ..owner_liveness import runs_named_by_live_work
from ..reservation_owners import OWNER_SETTLE_GRACE
from .common import settle_absent_run_in_session

_LOGGER = logging.getLogger(__name__)
_OWNER_ENDED = "owner-ended"


class RunEndings(Protocol):
    def end_unfinished_run_in_session(
        self, session: Session, run: RecipeRun, now: datetime, *, version_before: int
    ) -> RunState:
        """Queue the run's exact Stop (``stopping``), else end it as ``lost``."""
        ...


def _utc(value: datetime) -> datetime:
    # Stored timestamps can come back naive (SQLite); they are UTC either way.
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def never_launched(session: Session, run_id: str) -> bool:
    """Whether the run's Starts ended and none of their orders was ever issued.

    A Start enqueues its first phase only; later phases follow a completed
    earlier phase, so all-unissued orders prove that no rank launched. A run
    with no Start job left (pruned) or no orders is unproven.
    """

    starts = tuple(
        session.scalars(
            select(Job.id).where(
                Job.kind == WireAgentOperation.RECIPE_START.value,
                Job.payload["owner_kind"].as_string() == "run",
                Job.payload["owner_id"].as_string() == run_id,
            )
        )
    )
    if not starts:
        return False
    attempts = tuple(
        session.scalars(
            select(AgentOperation.current_attempt).where(
                AgentOperation.parent_job_id.in_(starts),
                AgentOperation.kind == WireAgentOperation.RECIPE_START.value,
            )
        )
    )
    return bool(attempts) and all(attempt == 0 for attempt in attempts)


def end_orphaned_runs_in_session(
    session: Session, endings: RunEndings, now: datetime
) -> tuple[bool, bool]:
    """End the in-flight runs no live work names: ``(worked, queued a Stop)``."""

    rows = {
        run_id: (version, updated_at)
        for run_id, version, updated_at in session.execute(
            select(RecipeRun.id, RecipeRun.version, RecipeRun.updated_at)
            .where(RecipeRun.state.in_(IN_FLIGHT_RUN_STATES))
            .order_by(RecipeRun.created_at, RecipeRun.id)
        )
    }
    named = runs_named_by_live_work(session, rows)
    worked = queued = False
    for run_id, (seen, updated_at) in rows.items():
        if run_id in named or _utc(updated_at) + OWNER_SETTLE_GRACE > now:
            continue  # owned, or a reader may still be racing the owner's commit
        run = session.get(RecipeRun, run_id, populate_existing=True)
        if run is None or run.state not in IN_FLIGHT_RUN_STATES or run.version != seen:
            continue  # taken over since it was read
        if never_launched(session, run_id):
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank, RunNode.node_id)
                )
            )
            settle_absent_run_in_session(session, run, nodes, now)
            outcome = RunState.STOPPED
        else:
            outcome = endings.end_unfinished_run_in_session(
                session, run, now, version_before=seen
            )
            queued = queued or outcome == RunState.STOPPING
        log_event(
            _LOGGER,
            "recipe.run.owner-ended",
            service="control-routes",
            kind="run",
            id=run_id,
            reason=_OWNER_ENDED,
            outcome=str(outcome),
        )
        worked = True
    return worked, queued
