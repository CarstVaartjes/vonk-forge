"""Make a run-switch due the moment a child it waits on ends.

A run-switch waiting on a child has no due time, so only the ``recipes``
source's turn (once per worker rotation) looked at it again. Children end in
many places (an agent report, a lapsed-attempt sweep, an abandon, the recipe
worker, a model download), so the wake is not a callback at each of them: it
watches the rows a child is stored in and, once the transaction that ended one
has committed and returned its connection, makes every live run-switch that
names it due now. The worker's due-waits backstop then redrives it within a
step. The wake only writes the due clock; advancing stays with the worker.
``wake_owners`` is the one primitive: a finished background runtime-image
preparation (``reconcile_background``) wakes its owner through it too.

A lost wake (a skipped locked parent, a failed dispatch, a process exit) costs
only the old latency: the parent is still observed on the ``recipes`` turn.
A stale match is an early look that the advance answers idempotently.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker
from vonk_agent_protocol import LifecycleState

from .. import job_states, model_cache_states
from ..models import Job, ModelCacheOperation
from ..persisted_waits import schedule_wait
from .constants import _OPERATION_KINDS
from .planning_helpers import _aware

_LOGGER = logging.getLogger(__name__)
_ENDED = (LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED)
#: Every row a run-switch child is stored in, with its ended words: recipe
#: install/start/stop/uninstall, build, distribution and preflight children are
#: ``Job`` rows; a model download is a ``ModelCacheOperation``.
CHILD_ROWS: dict[type[object], frozenset[str]] = {
    Job: frozenset(job_states.words(*_ENDED)),
    ModelCacheOperation: frozenset(model_cache_states.words(*_ENDED)),
}
_LIVE = job_states.words(
    LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.OBSERVING
)
_STAGED = "vonk.child_wake.staged"
_COMMITTED = "vonk.child_wake.committed"
_INSTALLED = "_vonk_child_wake"


class ChildEndWake:
    """Wake run-switch owners of children ended through one session factory."""

    def __init__(
        self, sessions: sessionmaker[Session], clock: Callable[[], datetime]
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    @classmethod
    def install(
        cls, sessions: sessionmaker[Session], clock: Callable[[], datetime]
    ) -> ChildEndWake:
        """Bind once per session factory; a second binding keeps the first."""
        installed = getattr(sessions, _INSTALLED, None)
        if installed is not None:
            return installed
        wake = cls(sessions, clock)
        event.listen(sessions, "before_flush", wake._stage)
        event.listen(sessions, "after_commit", wake._committed)
        event.listen(sessions, "after_transaction_end", wake._dispatch)
        setattr(sessions, _INSTALLED, wake)
        return wake

    @staticmethod
    def _stage(session: Session, _context: object, _instances: object) -> None:
        for row in (*session.new, *session.dirty):
            ended = CHILD_ROWS.get(type(row))
            if ended is None or row.state not in ended:  # type: ignore[attr-defined]
                continue
            if row in session.new or inspect(row).attrs.state.history.added:
                session.info.setdefault(_STAGED, set()).add(row.id)  # type: ignore[attr-defined]

    @staticmethod
    def _committed(session: Session) -> None:
        staged = session.info.pop(_STAGED, None)
        if staged:
            session.info.setdefault(_COMMITTED, set()).update(staged)

    def _dispatch(self, session: Session, transaction: SessionTransaction) -> None:
        # The root transaction has ended and released its connection, so the
        # wake never holds a second one while the first is still checked out.
        if transaction.parent is not None:
            return
        session.info.pop(_STAGED, None)  # rolled back: nothing ended
        ended = session.info.pop(_COMMITTED, None)
        if ended:
            self.wake(frozenset(ended))

    def wake(self, child_ids: frozenset[str]) -> None:
        """Make each live run-switch waiting on one of ``child_ids`` due now."""
        try:
            with self._sessions() as session:
                waiting = session.execute(
                    select(
                        Job.id,
                        Job.result["child_operation_id"].as_string(),
                        Job.result["preflight"]["pending_jobs"],
                    ).where(Job.kind.in_(_OPERATION_KINDS), Job.state.in_(_LIVE))
                ).all()
            owners = frozenset(
                owner
                for owner, child, probes in waiting
                if child in child_ids
                or (
                    isinstance(probes, dict)
                    and not child_ids.isdisjoint(probes.values())
                )
            )
            now = self._clock()
        except Exception:
            _LOGGER.warning(
                "run-switch child-end wake deferred to the next worker turn",
                exc_info=True,
            )
            return
        wake_owners(self._sessions, owners, now)


def wake_owners(
    sessions: sessionmaker[Session], owner_ids: frozenset[str], now: datetime
) -> None:
    """Make each live run-switch in ``owner_ids`` due now; the one wake primitive.

    Never blocks: a parent locked by its own advance is skipped (that advance
    or the next turn sees what ended). A parent already due is left alone.
    Any failure is logged, never raised into the caller, whose own effect is
    already durable; the parent then waits for its ordinary turn.
    """
    if not owner_ids:
        return
    try:
        with sessions.begin() as session:
            for job in session.scalars(
                select(Job)
                .where(
                    Job.id.in_(sorted(owner_ids)),
                    Job.kind.in_(_OPERATION_KINDS),
                    Job.state.in_(_LIVE),
                )
                .order_by(Job.id)
                .with_for_update(skip_locked=True)
            ):
                due = job.next_action_at
                if due is None or _aware(due) > now:
                    schedule_wait(job, now)
    except Exception:
        _LOGGER.warning(
            "run-switch wake deferred to the next worker turn", exc_info=True
        )
