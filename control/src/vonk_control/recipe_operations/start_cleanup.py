"""Queue a failed Start's cleanup Stop after the result transaction commits.

The agent result that fails a Start is recorded in one short transaction. Queueing
the cleanup Stop needs admission's node locks (NOWAIT), so doing it inside that
transaction made a busy admission roll the whole agent result back. The result
transaction therefore only records the run as ``stopping`` and stages the Stop
here; once it has committed and returned its connection, a fresh transaction
queues the Stop, and only while the run is still ``stopping`` at the staged
version. A busy admission, a lost dispatch or a process exit costs nothing but
latency: the run stays ``stopping`` and the orphaned-run reaper queues the Stop
after its settle grace. The Stop's request id makes a second queueing a no-op.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction
from vonk_agent_protocol.state_machines import RunState

from ..admission_locking import set_admission_timeout
from ..models.execution import RecipeRun
from .run_transitions import end_unfinished_run

if TYPE_CHECKING:
    from .service import RecipeOperationService

_LOGGER = logging.getLogger(__name__)
_STAGED = "vonk.start_cleanup.staged"
_COMMITTED = "vonk.start_cleanup.committed"
_INSTALLED = "_vonk_start_cleanup"


@dataclass(frozen=True)
class StartCleanup:
    """One staged cleanup Stop of a failed Start."""

    run_id: str
    version: int
    actor: str
    request_id: str
    ordinal: int
    note: str


def stage(session: Session, cleanup: StartCleanup) -> None:
    """Queue ``cleanup`` for dispatch once ``session``'s transaction commits."""

    session.info.setdefault(_STAGED, []).append(cleanup)


class StartCleanupDispatch:
    """Dispatch staged cleanups of runs ended through one session factory."""

    def __init__(self, service: RecipeOperationService) -> None:
        self._service = service

    @classmethod
    def install(cls, service: RecipeOperationService) -> StartCleanupDispatch:
        """Bind once per session factory; a second binding keeps the first."""

        sessions = service._sessions
        installed = getattr(sessions, _INSTALLED, None)
        if installed is not None:
            return installed
        dispatch = cls(service)
        event.listen(sessions, "after_commit", dispatch._committed)
        event.listen(sessions, "after_transaction_end", dispatch._dispatch)
        setattr(sessions, _INSTALLED, dispatch)
        return dispatch

    @staticmethod
    def _committed(session: Session) -> None:
        staged = session.info.pop(_STAGED, None)
        if staged:
            session.info.setdefault(_COMMITTED, []).extend(staged)

    def _dispatch(self, session: Session, transaction: SessionTransaction) -> None:
        # The root transaction has ended and released its connection.
        if transaction.parent is not None:
            return
        session.info.pop(_STAGED, None)  # rolled back: nothing was recorded
        for cleanup in session.info.pop(_COMMITTED, ()):
            self.run(cleanup)

    def run(self, cleanup: StartCleanup) -> None:
        """Queue the Stop for ``cleanup`` if its run still waits for it."""

        service = self._service
        try:
            with service._sessions.begin() as session:
                set_admission_timeout(session)
                # No row lock: the run's version fences a concurrent change (a
                # stale write fails and is left to the reaper).
                run = session.get(RecipeRun, cleanup.run_id)
                if (
                    run is None
                    or run.state != RunState.STOPPING
                    or run.version != cleanup.version
                ):
                    return
                queued = end_unfinished_run(
                    service,
                    session,
                    run,
                    service._clock(),
                    actor=cleanup.actor,
                    request_id=cleanup.request_id,
                    ordinal=cleanup.ordinal,
                    note=cleanup.note,
                )
        except Exception:
            _LOGGER.warning(
                "failed-start cleanup of run %s is left to the orphaned-run reaper",
                cleanup.run_id,
                exc_info=True,
            )
        else:
            if queued:
                service._agent_jobs.notify_available()
