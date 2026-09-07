"""Restart-safe advancement of pending recipe route publications."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import RecipeRun, RunNode
from .recipe_routes import RecipeRouteNotReady, RecipeRouteService


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
        routes: RecipeRouteService,
        *,
        clock: Callable[[], datetime],
        recoveries: _RecoveryCoordinator | None = None,
        fleet_profiles: _FleetProfileCoordinator | None = None,
        run_switches: _RunSwitchCoordinator | None = None,
    ) -> None:
        self._sessions = sessions
        self._routes = routes
        self._clock = clock
        self._recoveries = recoveries
        self._fleet_profiles = fleet_profiles
        self._run_switches = run_switches

    def tick(self) -> bool:
        progressed = False
        # Parent operations depend on lifecycle observations and published routes.
        # Give each coordinator a turn before servicing those dependencies.
        for coordinator in (self._fleet_profiles, self._run_switches, self._recoveries):
            if coordinator is not None:
                progressed = coordinator.tick() or progressed
        if self._expire_initial_observation_deadline():
            progressed = True
            if self._recoveries is not None:
                self._recoveries.tick()
        with self._sessions() as session:
            run_ids = tuple(
                session.scalars(
                    select(RecipeRun.id)
                    .where(
                        RecipeRun.state == "running",
                        RecipeRun.route_state == "pending",
                    )
                    .order_by(RecipeRun.created_at, RecipeRun.id)
                )
            )
        for run_id in run_ids:
            try:
                self._routes.publish_run(run_id)
            except RecipeRouteNotReady:
                continue
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                with self._sessions.begin() as session:
                    run = session.get(RecipeRun, run_id)
                    if run is not None and run.route_state == "pending":
                        run.route_state = "failed"
                        run.route_error = f"{type(error).__name__}: {error}"[:512]
                        run.updated_at = self._clock()
            return True
        return self._routes.maintain(renew_before_seconds=10) or progressed

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
                if (
                    not isinstance(run.plan, Mapping)
                    or run.plan.get("observation_schema_version") != 2
                ):
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
                run.route_error = "distributed start observation deadline elapsed"
                run.updated_at = now
                return True
        return False


__all__ = ["RecipeOperationWorker"]
