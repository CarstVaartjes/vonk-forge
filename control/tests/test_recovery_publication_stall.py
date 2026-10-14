"""A recovery route publication ends on a stalled rank reload, never on a clock."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from vonk_control.agent_operation_facts import START_STALL_WINDOW
from vonk_control.models import (
    AgentCertificate,
    AgentOperation,
    AgentOperationAttempt,
    RecipeRun,
)
from vonk_control.recipe_routes.service import RecipeRecoveryDeadlineError

from .test_recipe_operations import (
    _queued_distributed_recovery_restart,
    _recovery_deadline,
    _required,
)


def test_recovery_publication_waits_while_rank_reload_progresses(
    tmp_path: Path,
) -> None:
    """Catches DISTRIBUTED_START_TIMEOUT failing a recovery whose ranks still load.

    The accepted recovery budget is a stall window: progress its orders report
    extends it; a whole window without any ends the publication with that cause.
    """

    (sessions, _service, routes, _publisher, started, restart, worker, _nodes) = (
        _queued_distributed_recovery_restart(tmp_path)
    )
    deadline = _recovery_deadline(restart)
    progressed = deadline + timedelta(minutes=30)
    with sessions.begin() as session:
        order = _required(session.get(AgentOperation, worker.id))
        serial = _required(
            session.scalar(
                select(AgentCertificate.serial).where(
                    AgentCertificate.node_id == order.node_id
                )
            )
        )
        order.current_attempt = 1
        order.state = "running"
        session.add(
            AgentOperationAttempt(
                operation_id=order.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=progressed + timedelta(seconds=30),
                agent_certificate_serial=serial,
                state="running",
                progress={
                    "phase": "rank-launch",
                    "completed_bytes": 0,
                    "total_bytes_known": False,
                    "members": [],
                    "last_progress_at": progressed.isoformat(),
                },
            )
        )

    def enforce(now):
        routes._clock = lambda: now
        with sessions.begin() as session:
            run = _required(session.get(RecipeRun, started.owner_id))
            return routes._enforce_recovery_publication_deadline(session, run)

    # Forty minutes past the accepted budget, the reload is still progressing.
    still = enforce(deadline + timedelta(minutes=40))
    assert not isinstance(still, RecipeRecoveryDeadlineError)
    assert still is not None
    # A whole stall window after its last progress, it has stalled.
    ended = enforce(progressed + START_STALL_WINDOW + timedelta(seconds=1))
    assert isinstance(ended, RecipeRecoveryDeadlineError)
    with sessions() as session:
        run = _required(session.get(RecipeRun, started.owner_id))
        assert run.route_error is not None
        assert routes._recovery_job(session, run) is None
