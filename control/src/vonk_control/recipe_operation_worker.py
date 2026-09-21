"""Restart-safe advancement of pending recipe route publications."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from .models import RecipeRun, RunNode
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_plan,
)
from .recipe_routes import RecipeRouteNotReady, publication_is_temporary
from .recovery_policy import RecoveryPolicy

#: Bounded publication retry.  The first attempt is prompt so a momentary
#: supervisor hiccup does not delay a ready run, and the cap keeps a
#: persistent dependency failure from becoming a tight retry loop.  The
#: budget stays inside the Run/Switch final-verification window, so a
#: dependency that returns still converges without another operator command.
_ROUTE_PUBLICATION_RETRY = RecoveryPolicy(
    max_failures=6, base_delay_seconds=5, max_delay_seconds=60
)


class _RoutePublisher(Protocol):
    """The publication surface this worker drives.

    Narrowing it to the two operations the worker actually calls keeps the
    retry decision testable without substituting the real route service.
    """

    def publish_run(self, run_id: str) -> object: ...

    def maintain(self, *, renew_before_seconds: int = 10) -> bool: ...


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
    ) -> None:
        self._sessions = sessions
        self._routes = routes
        self._clock = clock
        self._recoveries = recoveries
        self._fleet_profiles = fleet_profiles
        self._run_switches = run_switches
        self._build_cleanup = build_cleanup
        self._retirement_cleanup = retirement_cleanup

    def tick(self) -> bool:
        progressed = False
        if self._build_cleanup is not None:
            progressed = self._build_cleanup()
        if self._retirement_cleanup is not None:
            progressed = self._retirement_cleanup() or progressed
        # Parent operations depend on lifecycle observations and published routes.
        # Give each coordinator a turn before servicing those dependencies.
        for coordinator in (self._fleet_profiles, self._run_switches, self._recoveries):
            if coordinator is not None:
                progressed = coordinator.tick() or progressed
        if self._expire_initial_observation_deadline():
            progressed = True
            if self._recoveries is not None:
                self._recoveries.tick()
        now = self._clock()
        with self._sessions() as session:
            run_ids = tuple(
                session.scalars(
                    select(RecipeRun.id)
                    .where(
                        RecipeRun.state == "running",
                        RecipeRun.route_state == "pending",
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
        for run_id in run_ids:
            try:
                self._routes.publish_run(run_id)
            except RecipeRouteNotReady:
                # Fail-closed: the candidate is waiting on current rank
                # evidence, which the next tick re-reads.  This is not a
                # failed attempt.
                continue
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                self._defer_publication(run_id, error)
            return True
        return self._routes.maintain(renew_before_seconds=10) or progressed

    def _defer_publication(self, run_id: str, error: BaseException) -> None:
        """Record one failed publication attempt without losing the run.

        A temporary dependency failure keeps the run pending at a durable
        next-attempt time so the existing worker converges once the dependency
        returns.  An unrecognised failure is not retried: it becomes one
        precise blocked reason for an explicit disposition, because the
        Controller cannot distinguish it from an invalid route contract.
        """

        now = self._clock()
        detail = f"{type(error).__name__}: {error}"[:512]
        temporary = publication_is_temporary(error)
        with self._sessions.begin() as session:
            run = session.get(RecipeRun, run_id, with_for_update=True)
            if run is None or run.route_state != "pending":
                return
            attempts = int(run.route_attempts or 0) + 1
            run.route_attempts = attempts
            if not temporary:
                run.route_state = "failed"
                run.route_error = detail
                run.route_next_attempt_at = None
                run.updated_at = now
                return
            due_at = _ROUTE_PUBLICATION_RETRY.next_attempt(run_id, attempts, now)
            if due_at is None:
                run.route_state = "failed"
                run.route_error = (
                    f"{detail} (route publication did not converge after "
                    f"{attempts} attempts)"
                )[:512]
                run.route_next_attempt_at = None
            else:
                run.route_error = detail
                run.route_next_attempt_at = due_at
            run.updated_at = now

    def _expire_initial_observation_deadline(self) -> bool:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("recipe operation worker clock must be timezone-aware")
        now = now.astimezone(UTC)
        with self._sessions.begin() as session:
            runs = tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(
                        RecipeRun.state == "running",
                        RecipeRun.route_state == "pending",
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
                    or not isinstance(node.observation_receipt_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", node.observation_receipt_sha256)
                    is None
                    or (
                        deadline is not None
                        and (
                            node.updated_at.replace(tzinfo=UTC)
                            if node.updated_at.tzinfo is None
                            or node.updated_at.utcoffset() is None
                            else node.updated_at.astimezone(UTC)
                        )
                        > (
                            deadline.replace(tzinfo=UTC)
                            if deadline.tzinfo is None or deadline.utcoffset() is None
                            else deadline.astimezone(UTC)
                        )
                    )
                )
                if not missing:
                    continue
                for node in missing:
                    node.state = "failed"
                    node.updated_at = now
                run.route_state = "withdrawn"
                run.route_error = "initial exact observation deadline elapsed"
                run.updated_at = now
                return True
        return False


__all__ = ["RecipeOperationWorker"]
