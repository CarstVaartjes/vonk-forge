"""A persisted queued load survives a missed wakeup and a worker restart."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import InvalidRequestReason, LifecycleState
from vonk_agent_protocol.agent_words import ProfileOperationKind
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileInput,
)
from vonk_control.fleet_profiles import FleetProfileInvalid
from vonk_control.jobs import JobService
from vonk_control.models import (
    AgentNode,
    FleetProfileApplication,
    FleetProfileSelection,
    RecipeBuild,
)
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.worker import Worker

from .test_profile_capacity_admission import _capacity_profile
from .test_profile_installed_execution import _profile_service
from .test_selected_profile_postgres import _NoopRoutes


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("maintenance_conflict", [False, True])
def test_due_queued_load_is_attempted_after_worker_restart(
    tmp_path, postgres_engine, blocked, maintenance_conflict, monkeypatch
):
    """Catches a due admission being excluded from the normal worker scan."""
    sessions, profiles, planner, profile, *_ = _capacity_profile(
        tmp_path, postgres_engine
    )
    if blocked:
        with sessions.begin() as session:
            build = session.scalar(select(RecipeBuild))
            assert build is not None
            build.state = LifecycleState.FAILED
    review = profiles.preview(profile.id)
    assert review.allowed != blocked
    pending = profiles._create_pending_application(
        review,
        request_key=str(uuid4()),
        actor="admin",
        operation_kind=ProfileOperationKind.APPLY.value,
    )
    due = pending.progress.admission_retry_at
    assert due is not None

    def restart(now, *, conflict=False):
        recovered, _ = _profile_service(sessions, planner._lifecycle)
        recovered._clock = lambda: now
        if conflict:
            # Production: a roster replay's request key names another plan.
            # That owner's refusal cannot prevent another due load's turn.
            def refuse_roster(_now):
                raise FleetProfileInvalid(
                    "Fleet profile request key was reused for another plan",
                    reason=InvalidRequestReason.CONFLICT,
                )

            monkeypatch.setattr(recovered, "_reconcile_selected_roster", refuse_roster)
        return Worker(
            JobService(sessions, clock=lambda: now),
            "queued-profile-test",
            {},
            recipes=RecipeOperationWorker(
                sessions, _NoopRoutes(), clock=lambda: now, fleet_profiles=recovered
            ),
        )

    # The first process exits before the durable retry comes due. No event or
    # API read is allowed to wake the replacement process's queued application.
    before = restart(due - timedelta(seconds=1))
    before.run_once()
    before.close()
    if blocked:
        with sessions.begin() as session:
            build = session.scalar(select(RecipeBuild))
            assert build is not None
            build.state = LifecycleState.SUCCEEDED
    restarted = restart(due + timedelta(seconds=1), conflict=maintenance_conflict)
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


@pytest.mark.parametrize("same_profile", [False, True])
def test_roster_repair_cannot_starve_due_admission(
    tmp_path, postgres_engine, same_profile
):
    """Catches maintenance replacing a newer load while its admission is busy."""
    sessions, profiles, planner, *_ = _capacity_profile(tmp_path, postgres_engine)
    idle = profiles.create(
        FleetProfileInput(name="Selected idle", assignments=[]), actor="admin"
    )
    selected = profiles.apply(idle.id, request_key=str(uuid4()), actor="admin")
    assert selected.state == LifecycleState.SUCCEEDED
    joined = "spk_" + "2" * 32
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=joined,
                state=NodeIdentityState.ACTIVE,
                architecture="linux-arm64",
            )
        )
    newer = (
        idle
        if same_profile
        else profiles.create(
            FleetProfileInput(name="Newest idle", assignments=[]), actor="admin"
        )
    )
    pending = profiles._create_pending_application(
        profiles.preview(newer.id),
        request_key=str(uuid4()),
        actor="admin",
        operation_kind=ProfileOperationKind.APPLY.value,
    )
    due = pending.progress.admission_retry_at
    assert due is not None
    recovered, _ = _profile_service(sessions, planner._lifecycle)
    recovered._clock = lambda: due - timedelta(seconds=1)
    worker = RecipeOperationWorker(
        sessions, _NoopRoutes(), clock=recovered._clock, fleet_profiles=recovered
    )
    # A heartbeat transaction holds the joined node while selected-profile
    # maintenance tries to bind its new roster. It must not monopolize ticks.
    with sessions.begin() as holder:
        assert holder.get(AgentNode, joined, with_for_update=True) is not None
        # Maintenance must yield to the newer load even during its backoff,
        # without manufacturing a later request or resetting the retry clock.
        for _ in range(2):
            assert not recovered._reconcile_selected_roster(recovered._clock())
        assert recovered.application(pending.id) == pending
        recovered._clock = lambda: due + timedelta(seconds=1)
        for _ in range(2):
            worker.tick()
        attempted = recovered.application(pending.id)
        assert attempted.state == LifecycleState.QUEUED, attempted.status_reason
        assert attempted.progress.admission_attempt > pending.progress.admission_attempt
        with sessions() as session:
            assert set(session.scalars(select(FleetProfileApplication.id))) == {
                selected.id,
                pending.id,
            }
            assert (
                session.get_one(FleetProfileSelection, 1).application_id == selected.id
            )
    next_due = attempted.progress.admission_retry_at
    assert next_due is not None
    recovered._clock = lambda: next_due + timedelta(seconds=1)
    worker.tick()
    completed = recovered.application(pending.id)
    assert not completed.progress.admission_pending, completed.status_reason
    assert completed.state == LifecycleState.SUCCEEDED
    with sessions() as session:
        assert session.get_one(FleetProfileSelection, 1).application_id == pending.id


def test_load_accepted_during_roster_preview_keeps_precedence(
    tmp_path, postgres_engine, monkeypatch
):
    """Catches a newer load arriving after the maintenance scan but before admission."""
    sessions, profiles, *_ = _capacity_profile(tmp_path, postgres_engine)
    idle = profiles.create(
        FleetProfileInput(name="Selected idle", assignments=[]), actor="admin"
    )
    selected = profiles.apply(idle.id, request_key=str(uuid4()), actor="admin")
    assert selected.state == LifecycleState.SUCCEEDED
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id="spk_" + "2" * 32,
                state=NodeIdentityState.ACTIVE,
                architecture="linux-arm64",
            )
        )
    preview = profiles.preview
    loads: list[FleetProfileApplicationView] = []

    def preview_then_accept(*args, **kwargs):
        reviewed = preview(*args, **kwargs)
        loads.append(
            profiles._create_pending_application(
                preview(idle.id),
                request_key=str(uuid4()),
                actor="admin",
                operation_kind=ProfileOperationKind.APPLY.value,
            )
        )
        return reviewed

    with monkeypatch.context() as injection:
        injection.setattr(profiles, "preview", preview_then_accept)
        assert profiles._reconcile_selected_roster(profiles._clock())
    pending = loads[0]
    with sessions() as session:
        assert session.get_one(FleetProfileSelection, 1).application_id == selected.id
    assert profiles.application(pending.id) == pending
    due = pending.progress.admission_retry_at
    assert due is not None
    profiles._clock = lambda: due + timedelta(seconds=1)
    profiles.tick()
    completed = profiles.application(pending.id)
    assert completed.state == LifecycleState.SUCCEEDED, completed.status_reason
    with sessions() as session:
        assert session.get_one(FleetProfileSelection, 1).application_id == pending.id
