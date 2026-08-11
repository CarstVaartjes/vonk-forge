"""Restart-safe advancement of pending recipe route publications."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import RecipeRun
from .recipe_routes import RecipeRouteService


class RecipeOperationWorker:
    _PRESENCE_ERROR = "recipe rank presence is unavailable"

    def __init__(
        self,
        sessions: sessionmaker[Session],
        routes: RecipeRouteService,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = sessions
        self._routes = routes
        self._clock = clock

    def tick(self) -> bool:
        with self._sessions() as session:
            run_id = session.scalar(
                select(RecipeRun.id)
                .where(
                    RecipeRun.state == "running",
                    RecipeRun.route_state == "pending",
                )
                .order_by(RecipeRun.created_at, RecipeRun.id)
                .limit(1)
            )
        if run_id is not None:
            try:
                self._routes.publish_run(run_id)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                with self._sessions.begin() as session:
                    run = session.get(RecipeRun, run_id)
                    if run is not None and run.route_state == "pending":
                        run.route_state = "failed"
                        run.route_error = f"{type(error).__name__}: {error}"[:512]
                        run.updated_at = self._clock()
            return True
        with self._sessions() as session:
            monitored = tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(
                        RecipeRun.state == "running",
                        RecipeRun.route_state.in_(("published", "withdrawn")),
                    )
                    .order_by(RecipeRun.created_at, RecipeRun.id)
                )
            )
        for run in monitored:
            present = self._routes.ranks_present(run.id)
            if run.route_state == "published" and not present:
                try:
                    self._routes.withdraw_run(run.id)
                except (OSError, RuntimeError, TypeError, ValueError) as error:
                    with self._sessions.begin() as session:
                        current = session.get(RecipeRun, run.id)
                        if current is not None and current.route_state == "published":
                            current.route_error = f"{type(error).__name__}: {error}"[:512]
                            current.updated_at = self._clock()
                    return True
                with self._sessions.begin() as session:
                    current = session.get(RecipeRun, run.id)
                    if current is not None and current.route_state == "withdrawn":
                        current.route_error = self._PRESENCE_ERROR
                        current.updated_at = self._clock()
                return True
            if (
                run.route_state == "withdrawn"
                and run.route_error == self._PRESENCE_ERROR
                and present
            ):
                try:
                    self._routes.publish_run(run.id)
                except (OSError, RuntimeError, TypeError, ValueError):
                    # Withdrawal is already the safe state. Retain the exact
                    # recovery marker so the next worker tick retries.
                    pass
                return True
        return False


__all__ = ["RecipeOperationWorker"]
