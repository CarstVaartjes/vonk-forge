"""A Spark's report that an ended run's process still runs reopens the run."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import RecipeRunObservationWire, RouteState, RunState

from ..logging import log_event
from ..models import RecipeRun, RunNode
from ..owner_liveness import run_has_live_operation
from ..persisted_waits import schedule_wait
from ..recipe_stop_payloads import durable_run_stop_payloads
from .common import _LOGGER, _now

ENDED_RUN_STATES = frozenset({RunState.FAILED, RunState.STOPPED})


def reopen_ended_runs_still_running(
    session: Session,
    node_id: str,
    observed_at: datetime,
    now: datetime,
    reports: Mapping[str, RecipeRunObservationWire],
) -> set[str]:
    """Record an ended run whose process this Spark still reports as ``lost``.

    A ``failed`` or ``stopped`` run holds no claim, and no plan stops it. A
    process it left running on a Spark is a claim that outlived its owner. The
    agent cannot stop that process by itself, because every runtime stop needs
    a Controller grant bound to an order. So the Controller records what the
    Spark reports: the run becomes ``lost``, which is stoppable. The next load
    on that Spark plans its exact Stop, and the run's disposition stays known,
    so the agent keeps reporting it.

    Nothing changes for a report:

    - that says the process is not running;
    - of another generation;
    - taken before the run or its rank last changed;
    - for a rank this Spark does not hold;
    - for a run a live operation still owns;
    - for a run no exact Stop can be built for (its installation or membership
      moved on): reopened, it would block every later load on that Spark.

    Returns the ids of the runs it reopened.
    """

    reopened: set[str] = set()
    for run_id, report in sorted(reports.items()):
        if not report.process_running:
            continue
        run = session.get(RecipeRun, run_id)
        if run is not None:
            run.version += 1
        if (
            run is None
            or run.state not in ENDED_RUN_STATES
            or run.run_generation != report.run_generation
            or _now(run.updated_at).astimezone(UTC) >= observed_at
        ):
            continue
        node = session.scalar(
            select(RunNode).where(RunNode.run_id == run_id, RunNode.node_id == node_id)
        )
        if (
            node is None
            or _now(node.updated_at).astimezone(UTC) >= observed_at
            or run_has_live_operation(session, run_id)
            or not _stoppable(session, run)
        ):
            continue
        ended = RunState(run.state)
        run.state = RunState.LOST
        run.route_state = RouteState.WITHDRAWN
        run.route_error = (
            f"Spark {node_id} reports the process still running after the run "
            f"was {ended.value}; exact Stop pending"
        )[:512]
        schedule_wait(run, None)
        run.stopped_at = None
        run.updated_at = now
        node.state = RunState.RUNNING
        node.observed_run_generation = run.run_generation
        node.observation_process_running = True
        node.observation_observed_at = observed_at
        node.updated_at = observed_at
        log_event(
            _LOGGER,
            "recipe.run.ended_process_running",
            service="control-api",
            run_id=run_id,
            node_id=node_id,
            ended_state=ended.value,
            run_generation=run.run_generation,
            observed_at=observed_at.isoformat(),
        )
        reopened.add(run_id)
    return reopened


def _stoppable(session: Session, run: RecipeRun) -> bool:
    """Whether a plan could build this run's exact Stop now."""

    try:
        durable_run_stop_payloads(
            session,
            run,
            tuple(session.scalars(select(RunNode).where(RunNode.run_id == run.id))),
            run_generation=run.run_generation,
            cancel_pending_start=True,
            allow_missing_nodes=False,
        )
    except ValueError:
        # No exact authority, or stored rows that do not parse: either way no
        # plan could stop it, and this report must not fail the others.
        return False
    return True
