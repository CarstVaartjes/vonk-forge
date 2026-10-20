"""Restart-safe advancement of pending recipe route publications."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import or_, select, true
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    TERMINAL_LIFECYCLE_STATES,
    AgentOperation,
    RouteState,
    RunState,
    SecurityRefusalError,
)

from . import job_states
from .categorized_errors import InvalidValue
from .failure_classification import error_detail
from .lifecycle.recipe_operation import RecipeOperationAdapter
from .logging import log_event
from .models import Job, RecipeRun, RunNode
from .persisted_waits import schedule_wait
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_plan,
)
from .recipe_lifecycle_contract import RecipeOperationResult
from .recipe_operations.constants import _INITIAL_OBSERVATION_GRACE_SECONDS
from .recipe_routes import (
    RecipeRouteNotReady,
    RecipeRouteOwnerBusy,
    publication_disposition,
)
from .recovery_policy import RECOVERY, ROUTE_RECOVERY, schedule
from .reservation_owners import OwnerKind, OwnerRef, owner
from .stall_window import stall_deadline

_LOGGER = logging.getLogger("vonk-control-worker")


class _RoutePublisher(Protocol):
    """The publication surface this worker drives.

    Narrowing it to the two operations the worker actually calls keeps the
    retry decision testable without substituting the real route service.
    """

    def publish_run(self, run_id: str) -> object: ...

    def maintain(self) -> bool: ...


class _RecoveryCoordinator(Protocol):
    def tick(self, run_id: str | None = None) -> bool: ...


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
        reconcile_routes: bool = True,
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
        self._reconcile_routes = reconcile_routes
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
        # Consecutive passes that found the shared route owner held elsewhere.
        self._route_owner_waits = 0
        self._route_owner_due: datetime | None = None

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
        if self._reconcile_routes:
            progressed = self.reconcile_routes() or progressed
        return progressed

    def reconcile_routes(self) -> bool:
        """Advance durable publications and maintenance in the gateway authority owner."""
        progressed = self._advance(self._publish_routes)
        return self._advance(self._maintain_routes) or progressed

    def redrive_run(self, run_id: str) -> bool:
        progressed = (
            self._recoveries.tick(run_id) if self._recoveries is not None else False
        )
        progressed = self._expire_initial_observation_deadline(run_id) or progressed
        with self._sessions() as session:
            run = session.get(RecipeRun, run_id)
            publish = (
                run is not None
                and run.state == RunState.RUNNING
                and run.route_state == RouteState.PENDING
            )
        return (
            self._with_route_owner(lambda: self._publish_runs((run_id,)))
            if publish and self._reconcile_routes
            else False
        ) or progressed

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

    def _with_route_owner(self, task: Callable[[], bool]) -> bool:
        """Run one pass that needs the shared route publication owner.

        Every run and the maintenance pass take the same singleton row, so a
        held owner ends the pass and is not progress: trying the next run would
        only meet the same holder. The next pass waits a growing, jittered step
        that resets once the owner is won, and each wait is logged once.
        """

        now = self._clock()
        if self._route_owner_due is not None and now < self._route_owner_due:
            return False
        try:
            result = task()
        except RecipeRouteOwnerBusy as error:
            self._route_owner_waits += 1
            self._route_owner_due = RECOVERY.next_attempt(
                "recipe.route.owner", self._route_owner_waits, now, ongoing_intent=True
            )
            log_event(
                _LOGGER,
                "recipe.route.owner_busy",
                service="control-worker",
                retry_at=self._route_owner_due,
                reason=str(error),
            )
            return False
        self._route_owner_waits = 0
        self._route_owner_due = None
        return result

    def _maintain_routes(self) -> bool:
        return self._with_route_owner(self._routes.maintain)

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
                            RecipeRun.next_action_at.is_(None),
                            RecipeRun.next_action_at <= now,
                        ),
                    )
                    .order_by(RecipeRun.created_at, RecipeRun.id)
                )
            )
        if not run_ids:
            return False
        return self._with_route_owner(lambda: self._publish_runs(run_ids))

    def _publish_runs(self, run_ids: tuple[str, ...]) -> bool:
        progressed = False
        for run_id in run_ids:
            try:
                self._routes.publish_run(run_id)
            except RecipeRouteOwnerBusy:
                raise
            except SecurityRefusalError as error:
                self._end_publication(run_id, error)
                progressed = True
                continue
            except RecipeRouteNotReady as error:
                # Publication evidence that is not there yet: a wait on the
                # one retry clock, so restart cannot turn it into a hot loop
                # and it never spends the budget of a serving route.
                self._defer_publication(run_id, error)
                progressed = True
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
        serving model permanently unlisted.  The error's one disposition
        decides: a wait never spends an attempt, a deterministic error ends the
        publication observation at once with its cause, and only an
        unconfirmed publication spends one.  Standing serving intent is
        reconciled by independent, durable maintenance epochs.
        """

        now = self._clock()
        with self._sessions.begin() as session:
            run = session.get(RecipeRun, run_id, with_for_update=True)
            if run is None or run.route_state != RouteState.PENDING:
                return
            step = schedule(
                ROUTE_RECOVERY,
                run_id,
                int(run.route_attempts or 0),
                publication_disposition(error),
                now,
            )
            run.route_attempts = step.failures
            run.route_error = error_detail(error, limit=512)
            schedule_wait(run, step.next_at, now=now, detail=run.route_error)
            if run.next_action_at is None:
                # End publication observation, never invent a remote stop.
                run.route_state = RouteState.FAILED
                # Initial observation has its own persisted lifetime. Publication
                # exhaustion cannot erase it before the expiry pass reconciles it.
                # End only this owner's route-observation job. No rank stop or
                # failed workload is inferred from lost recovery metadata.
                for job in session.scalars(
                    select(Job).where(Job.kind == AgentOperation.RECIPE_START)
                ):
                    # A job that already ended keeps its outcome: the run's
                    # route state and error are the evidence of the failure.
                    if (
                        isinstance(job.payload, Mapping)
                        and job.payload.get("owner_id") == run.id
                        and job.payload.get("recovery") is not None
                        and not job_states.means(job.state, *TERMINAL_LIFECYCLE_STATES)
                    ):
                        RecipeOperationAdapter().finish(
                            job, now, failed=True, reason=str(error)
                        )
                        job.result = RecipeOperationResult(
                            successful_nodes=[],
                            failed_nodes=[],
                            node_evidence={},
                            recovery_error=(str(error) or type(error).__name__)[:512],
                        ).model_dump(mode="json", exclude_none=True)
                        job.updated_at = now
            run.updated_at = now

    def _end_publication(self, run_id: str, error: BaseException) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeRun, run_id, with_for_update=True)
            if run is not None and run.route_state == RouteState.PENDING:
                run.route_state = RouteState.WITHDRAWN
                schedule_wait(run, None)
                run.route_error = str(error)[:512]
                run.updated_at = self._clock()

    def _expire_initial_observation_deadline(self, run_id: str | None = None) -> bool:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise InvalidValue("recipe operation worker clock must be timezone-aware")
        now = now.astimezone(UTC)
        with self._sessions.begin() as session:
            runs = tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(RecipeRun.id == run_id if run_id is not None else true())
                    .where(
                        RecipeRun.state == RunState.RUNNING,
                        or_(
                            RecipeRun.route_state == RouteState.PENDING,
                            (
                                (RecipeRun.route_state == RouteState.FAILED)
                                & RecipeRun.observation_deadline_at.is_not(None)
                            ),
                        ),
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
                if any(
                    owner(
                        session, OwnerRef(OwnerKind.SPARK_RETURN, node.node_id), now
                    ).live
                    for node in missing
                ):
                    # A Spark that is away owns the late telemetry: the route
                    # waits for it (its window restarts) instead of vanishing.
                    run.observation_deadline_at = stall_deadline(
                        now, None, timedelta(seconds=_INITIAL_OBSERVATION_GRACE_SECONDS)
                    )
                    run.updated_at = now
                    return True
                # Missing telemetry from a present Spark ends the observation
                # episode; it proves neither rank failure nor a stopped process
                # or free capacity.
                run.route_state = RouteState.WITHDRAWN
                schedule_wait(run, None)
                run.observation_deadline_at = None
                run.route_error = "initial exact observation deadline elapsed"
                run.updated_at = now
                return True
        return False


__all__ = ["RecipeOperationWorker"]
