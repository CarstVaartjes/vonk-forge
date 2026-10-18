"""A persisted queued load survives a missed wakeup and a worker restart."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy import select
from vonk_agent_protocol import LifecycleState
from vonk_agent_protocol.agent_words import ProfileOperationKind
from vonk_control.jobs import JobService
from vonk_control.models import FleetProfileApplication
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.worker import Worker

from .test_profile_capacity_admission import _capacity_profile
from .test_profile_installed_execution import _profile_service


def test_due_queued_load_is_attempted_after_worker_restart(tmp_path, postgres_engine):
    """Catches a due admission being excluded from the normal worker scan."""
    sessions, profiles, planner, profile, *_ = _capacity_profile(tmp_path, postgres_engine)
    pending = profiles._create_pending_application(
        profiles.preview(profile.id),
        request_key=str(uuid4()),
        actor="admin",
        operation_kind=ProfileOperationKind.APPLY,
    )
    due = pending.progress.admission_retry_at
    assert due is not None

    def restart(now):
        recovered, _ = _profile_service(sessions, planner._lifecycle)
        recovered._clock = lambda: now
        routes = Mock()
        routes.maintain.return_value = False
        return Worker(
            JobService(sessions, clock=lambda: now),
            "queued-profile-test",
            {},
            recipes=RecipeOperationWorker(
                sessions, routes, clock=lambda: now, fleet_profiles=recovered
            ),
        )

    # The first process exits before the durable retry comes due. No event or
    # API read is allowed to wake the replacement process's queued application.
    before = restart(due - timedelta(seconds=1))
    before.run_once()
    before.close()
    restarted = restart(due + timedelta(seconds=1))
    try:
        restarted.run_once()
    finally:
        restarted.close()
    with sessions() as session:
        row = session.get(FleetProfileApplication, pending.id)
        assert row is not None
        assert row.state in (LifecycleState.QUEUED, LifecycleState.RUNNING)
        assert not row.progress["admission_pending"], row.status_reason
        assert list(session.scalars(select(FleetProfileApplication.id))) == [pending.id]
