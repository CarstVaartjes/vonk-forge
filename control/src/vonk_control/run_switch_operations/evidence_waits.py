"""A wait on evidence is checked again when the evidence lands, never on a clock.

After a Stop, admission needs inventory a Spark collected after that stop
(``executor_preflight._require_post_stop_inventory``).  The Spark collects it
on its own cadence, so a retry clock can only guess: a guess that is early
looks again before the evidence can exist, one that is late leaves the load
waiting after it arrived.  Instead the hold records which Spark it waits on and
the newest collection it already judged (``awaited_inventory``), and every
tick makes such a wait due at once when that Spark has reported a newer
collection.  Its own retry time is only the resync for a Spark that stays
silent.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import LifecycleState

from .. import job_states
from ..models import Job, NodeInventorySnapshot
from ..stored_json import read_row_column
from .constants import _OPERATION_KINDS
from .errors import RunSwitchPostStopEvidencePending
from .planning_helpers import _aware
from .result_helpers import _parse_persisted_result, _persisted_result


class RunSwitchPostStopInventoryPending(RunSwitchPostStopEvidencePending):
    """A Spark has not reported inventory collected after a stop yet.

    ``observed_after`` is the newest collection already judged, never earlier
    than ``collected_after``: only a newer one from ``node_id`` can settle it.
    """

    def __init__(
        self,
        message: str,
        *,
        node_id: str,
        collected_after: datetime,
        observed_after: datetime,
    ) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.observed_after = max(collected_after, observed_after)


def wake_evidence_waits(sessions: sessionmaker[Session], now: datetime) -> bool:
    """Make every post-stop wait whose awaited inventory landed due now."""

    woke = False
    with sessions.begin() as session:
        waiting = session.scalars(
            select(Job)
            .where(
                Job.kind.in_(_OPERATION_KINDS),
                Job.state.in_(
                    job_states.words(
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.OBSERVING,
                    )
                ),
                Job.result["awaited_inventory"].as_string().is_not(None),
            )
            .order_by(Job.id)
            .with_for_update()
        )
        for job in waiting:
            stored = read_row_column(job, "result")
            # A damaged result is its owner's to resolve; nothing is due here.
            progress = _parse_persisted_result(stored)
            if progress is None:
                continue
            awaited = progress.awaited_inventory
            due = progress.observation_due_at
            if (
                awaited is None
                or progress.retry_reason != RunSwitchPostStopEvidencePending.code
                or due is None
                or _aware(due) <= now
            ):
                continue
            landed = session.scalar(
                select(NodeInventorySnapshot.id)
                .where(
                    NodeInventorySnapshot.node_id == awaited.node_id,
                    NodeInventorySnapshot.observed_at > _aware(awaited.observed_after),
                )
                .limit(1)
            )
            if landed is None:
                continue
            progress.observation_due_at = now
            job.result = _persisted_result(progress)
            woke = True
    return woke
