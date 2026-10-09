"""Restart-safe advancement of pending recipe route publications."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import RouteState, RunState

from .categorized_errors import InvalidValue
from .models import RecipeRun, RunNode
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_plan,
)
from .recipe_routes import RecipeRouteNotReady
from .recovery_policy import RecoveryPolicy

#: Capped publication retry.  The first attempt is prompt so a momentary
#: supervisor hiccup does not delay a ready run, and the cap keeps a
#: persistent failure from becoming a tight retry loop.  A running run keeps
#: retrying for as long as it serves, so a dependency or evidence problem that
#: clears converges without another operator command.
_ROUTE_PUBLICATION_RETRY = RecoveryPolicy(
    max_failures=6, base_delay_seconds=5, max_delay_seconds=60
)
_LOGGER = logging.getLogger("vonk-control-worker")


class _RoutePublisher(Protocol):
    """The publication surface this worker drives.

    Narrowing it to the two operations the worker actually calls keeps the
    retry decision testable without substituting the real route service.
    """

    def publish_run(self, run_id: str) -> object: ...

    def maintain(self) -> bool: ...


class _RecoveryCoordinator(Protocol):
    def tick(self) -> bool: ...


class _FleetProfileCoordinator(Protocol):
    def tick(self) -> bool: ...


class _RunSwitchCoordinator(Protocol):
    def tick(self) -> bool: ...


class RecipeOperationWorker:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        routes: _RoutePublisher,
        *,
        clock: Callable[[], datetime],
        recoveries: _RecoveryCoordinator | None = None,
        fleet_profiles: _FleetProfileCoordinator | None = None,
        run_switches: _RunSwitchCoordinator | None = None,
        build_cleanup: Callable[[], bool] | None = None,
        retirement_cleanup: Callable[[], bool] | None = None,
        stop_admission_cleanup: Callable[[], bool] | None = None,
        residue_cleanup: Callable[[], bool] | None = None,
        order_reconcile: Callable[[], bool] | None = None,
    ) -> None:
        self._sessions = sessions
        self._routes = routes
        self._clock = clock
        self._recoveries = recoveries
        self._fleet_profiles = fleet_profiles
        self._run_switches = run_switches
        self._build_cleanup = build_cleanup
        self._retirement_cleanup = retirement_cleanup
        self._stop_admission_cleanup = stop_admission_cleanup
        self._residue_cleanup = residue_cleanup
        self._order_reconcile = order_reconcile
        self._retry_at: dict[Callable[[], bool], datetime] = {}

    def tick(self) -> bool:
        progressed = False
        # Preserve observation-before-admission ordering, but a damaged owner
        # cannot suppress a turn for another owner. Each call ends this turn;
        # the worker cadence re-observes it after its dependencies recover.
        for task in (
            self._order_reconcile,
            self._stop_admission_cleanup,
            self._build_cleanup,
            self._retirement_cleanup,
            self._residue_cleanup,
            self._fleet_profiles.tick if self._fleet_profiles is not None else None,
            self._run_switches.tick if self._run_switches is not None else None,
            self._recoveries.tick if self._recoveries is not None else None,
        ):
            progressed = self._advance(task) or progressed
        if self._advance(self._expire_initial_observation_deadline):
            progressed = True
            if self._recoveries is not None:
                self._advance(self._recoveries.tick)
        progressed = self._advance(self._publish_routes) or progressed
        return self._advance(self._routes.maintain) or progressed

    def _advance(self, task: Callable[[], bool] | None) -> bool:
        if task is None:
            return False
        now = self._clock()
        due = self._retry_at.get(task)
        if due is not None and now < due:
            return False
        try:
            result = task()
            self._retry_at.pop(task, None)
            return result
        except Exception:
            self._retry_at[task] = now + timedelta(seconds=5)
            _LOGGER.exception(
                "recipe coordinator observation failed",
                extra={
                    "owner": task.__qualname__
                    if hasattr(task, "__qualname__")
                    else type(task).__name__
                },
            )
            return False

    def _publish_routes(self) -> bool:
        now = self._clock()
        with self._sessions() as session:
            run_ids = tuple(
                session.scalars(
                    select(RecipeRun.id)
                    .where(
                        RecipeRun.state == RunState.RUNNING,
                        RecipeRun.route_state == RouteState.PENDING,
                        # A run that failed publication temporarily holds
                        # ``pending`` and is only due once its recorded
                        # next-attempt time arrives; an untouched run has no
                        # recorded attempt at all.
                        or_(
                            RecipeRun.route_next_attempt_at.is_(None),
                            RecipeRun.route_next_attempt_at <= now,
                        ),
                    )
                    .order_by(RecipeRun.created_at, RecipeRun.id)
                )
            )
        progressed = False
        for run_id in run_ids:
            try:
                self._routes.publish_run(run_id)
            except RecipeRouteNotReady:
                # Fail-closed: the candidate is waiting on current rank
                # evidence, which the next tick re-reads.  This is not a
                # failed attempt.
                continue
            except Exception as error:  # noqa: BLE001 - failure is scoped to this run
                try:
                    self._defer_publication(run_id, error)
                    progressed = True
                except Exception:
                    _LOGGER.exception(
                        "route publication observation failed", extra={"run_id": run_id}
                    )
                continue
            return True
        return progressed

    def _defer_publication(self, run_id: str, error: BaseException) -> None:
        """Record one failed publication attempt and schedule the next one.

        The run stays ``pending`` with its latest reason while it is running,
        so the route converges once the cause clears instead of leaving a
        serving model permanently unlisted.
        """

        now = self._clock()
        with self._sessions.begin() as session:
            run = session.get(RecipeRun, run_id, with_for_update=True)
            if run is None or run.route_state != RouteState.PENDING:
                return
            attempts = int(run.route_attempts or 0) + 1
            run.route_attempts = attempts
            run.route_error = f"{type(error).__name__}: {error}"[:512]
            run.route_next_attempt_at = _ROUTE_PUBLICATION_RETRY.next_attempt(
                run_id, attempts, now, ongoing_intent=True
            )
            run.updated_at = now

    def _expire_initial_observation_deadline(self) -> bool:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise InvalidValue("recipe operation worker clock must be timezone-aware")
        now = now.astimezone(UTC)
        with self._sessions.begin() as session:
            runs = tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(
                        RecipeRun.state == RunState.RUNNING,
                        RecipeRun.route_state == RouteState.PENDING,
                    )
                    .order_by(RecipeRun.created_at, RecipeRun.id)
                    .with_for_update(of=RecipeRun)
                )
            )
            if not runs:
                return False
            for run in runs:
                try:
                    exact_observations = (
                        parse_stored_run_plan(run.plan).observation_schema_version == 2
                    )
                except RecipeExecutionContractError:
                    exact_observations = False
                if not exact_observations:
                    continue
                deadline = run.observation_deadline_at
                if deadline is not None and now < (
                    deadline.replace(tzinfo=UTC)
                    if deadline.tzinfo is None or deadline.utcoffset() is None
                    else deadline.astimezone(UTC)
                ):
                    continue
                nodes = tuple(
                    session.scalars(
                        select(RunNode)
                        .where(RunNode.run_id == run.id)
                        .order_by(RunNode.rank)
                        .with_for_update(of=RunNode)
                    )
                )
                missing = tuple(
                    node
                    for node in nodes
                    if node.observed_run_generation != run.run_generation
                    or node.observation_observed_at is None
                )
                if not missing:
                    continue
                for node in missing:
                    node.state = RunState.FAILED
                    node.observation_process_running = None
                    node.observation_failure_diagnostics = None
                    node.observation_observed_at = None
                    node.updated_at = now
                run.route_state = RouteState.WITHDRAWN
                run.route_error = "initial exact observation deadline elapsed"
                run.updated_at = now
                return True
        return False


__all__ = ["RecipeOperationWorker"]
