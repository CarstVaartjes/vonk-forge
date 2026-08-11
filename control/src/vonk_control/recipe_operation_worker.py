"""Restart-safe advancement of pending recipe route publications."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .models import RecipeRun
from .recipe_routes import RecipeRouteService


class RecipeOperationWorker:
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
        if run_id is None:
            return self._routes.maintain()
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


__all__ = ["RecipeOperationWorker"]
