"""Foreground fleet completion retains exact offline cleanup and physical claims."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    LifecycleState,
    OutcomeDone,
    OutcomeKind,
    ProjectionCode,
    RecipeStopResult,
    ReservationState,
    ResourceBlockerCode,
    RunState,
    canonical_message,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
)
from vonk_control.job_documents import RecipeStopParent
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Job,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.offline_stops import is_deferred_stop
from vonk_control.run_switch_contract import RunSwitchApplyRequest, RunSwitchOperation

from .agent_fences import fenced_operation
from .non_blocking import assert_ended_without_blocking
from .test_fleet_profiles import _uuid
from .test_profile_continuity_postgres import (
    _assessment_with_promises,
    _SQLCancellationAdapter,
)
from .test_recipe_operations import (
    NOW,
    _issue_exact_stop_grant,
    installed_recipe,
    setup_services,
    started_recipe,
)
from .test_run_switch_operations import RecordingArtifactExecutor, _request, _service


@pytest.mark.parametrize("through_profile", [False, True])
def test_offline_stop_ends_admits_healthy_work_and_reconciles_exact_stop(
    postgres_engine, tmp_path, through_profile
):
    """Catches fleet waits, discounting offline claims, and orphaning reconnect Stops."""
    sessions, lifecycle, _, mapping, build, nodes = setup_services(
        tmp_path, engine=postgres_engine, nodes=2, mapping_node_count=1
    )
    installation = installed_recipe(
        lifecycle, mapping, build, nodes[:1], request_id=_uuid(19100)
    )
    run = started_recipe(
        sessions, lifecycle, installation.owner_id, nodes[:1], request_id=_uuid(19101)
    )
    clock = [NOW]
    lifecycle._clock = lambda: clock[0]
    with sessions.begin() as session:
        offline = session.get(AgentNode, nodes[0])
        healthy = session.get(AgentNode, nodes[1])
        assert offline is not None and healthy is not None
        offline.last_seen_at = NOW - timedelta(hours=2)
        healthy.last_seen_at = NOW
    coordinator = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    coordinator._clock = lambda: clock[0]
    profiles = FleetProfileService(
        sessions,
        clock=lambda: clock[0],
        switch_adapter=_SQLCancellationAdapter(),
        assessment_provider=_assessment_with_promises,
    )
    profiles._switch_adapter = RunSwitchFleetProfileAdapter(sessions, coordinator)
    if through_profile:
        idle = profiles.create(
            FleetProfileInput(name="clear offline load", assignments=[]), actor="admin"
        )
        operation = profiles.apply(idle.id, request_key=_uuid(19102), actor="admin")
    else:
        plan = lifecycle.preview_stop(run.owner_id)
        operation = lifecycle.stop(
            run.owner_id,
            plan_digest=plan.plan_digest,
            actor="admin",
            request_id=_uuid(19102),
        )

    def end(original):
        for _ in range(16):
            clock[0] += timedelta(seconds=2)
            profiles.tick()
            coordinator.tick()
            lifecycle.reconcile_pending_service_stops()
        return (
            profiles.application(original.id)
            if through_profile
            else lifecycle.get(original.id)
        )

    def fresh(_world):
        request = _request(sessions, nodes[1]).model_copy(update={"alias": "healthy"})
        return coordinator.apply_run(
            RunSwitchApplyRequest(**request.model_dump(), request_key=_uuid(19103)),
            actor="admin",
        )

    def request_key(receipt):
        key = getattr(receipt, "request_key", None)
        if key is not None:
            return key
        with sessions() as stored_session:
            row = stored_session.get(Job, receipt.id)
            assert row is not None
            return row.request_id

    with sessions() as session:
        ended, admitted = assert_ended_without_blocking(
            session,
            operation,
            end=end,
            fresh=fresh,
            request_key=request_key,
        )
    assert ended.state == LifecycleState.SUCCEEDED
    assert isinstance(admitted, RunSwitchOperation)
    assert admitted.node_ids == [nodes[1]]
    with sessions() as session:
        stop = session.scalar(select(Job).where(Job.kind == "recipe.stop"))
        assert stop is not None and stop.state == LifecycleState.SUCCEEDED
        parent = RecipeStopParent.model_validate_json(canonical_message(stop.payload))
        assert parent.offline_stop_intent is not None
        assert parent.offline_stop_intent.code == ProjectionCode.NODE_OFFLINE
        assert parent.offline_stop_intent.node_ids == [nodes[0]]
        pending = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == stop.id)
        )
        assert pending is not None and pending.state == LifecycleState.QUEUED
        assert is_deferred_stop(stop, pending)
        exact_payload = pending.payload
        pending.payload = {**exact_payload, "run_id": _uuid(19199)}
        assert not is_deferred_stop(stop, pending)
        pending.payload = exact_payload
        pending_id = pending.id
        stored = session.get(RecipeRun, run.owner_id)
        assert (
            stored is not None
            and stored.state == RunState.STOPPING
            and stored.stopped_at is None
        )
        assert (
            session.scalar(
                select(ResourceReservation.id).where(
                    ResourceReservation.owner_id == run.owner_id,
                    ResourceReservation.node_id == nodes[0],
                    ResourceReservation.state == ReservationState.ACTIVE,
                )
            )
            is not None
        )
    # A reviewed replacement must not discount the retained offline claim,
    # even if it explicitly plans to stop this run. Healthy-node work above is
    # already admitted, so this is a per-rank capacity fence, not a fleet gate.
    replacement = lifecycle.preview_run(
        installation.owner_id,
        alias="offline replacement",
        released_run_ids=(run.owner_id,),
    )
    assert not replacement.allowed
    assert any(
        blocker.code == ResourceBlockerCode.RESIDENT_USAGE_UNKNOWN
        and run.owner_id in blocker.detail
        for node in replacement.nodes
        for blocker in node.blockers
    )
    # A new service instance receives the normal authenticated poll, issues the
    # production exact-plan grant, and consumes its fenced Stop receipt.
    claim, payload, _grant = _issue_exact_stop_grant(
        sessions, node_id=nodes[0], certificate_serial="serial-0", grant_now=clock[0]
    )
    assert payload.run_id == run.owner_id
    assert fenced_operation(sessions, claim).id == pending_id
    jobs = AgentJobService(sessions, clock=lambda: clock[0])
    jobs.set_result_consumer(lifecycle.consume_agent_result)
    jobs.heartbeat(claim.fence, None, lease_seconds=600)
    jobs.record_result(
        AgentResult(
            fence=claim.fence,
            state=AgentResultState.SUCCEEDED,
            result=OutcomeDone(kind=OutcomeKind.DONE, result=RecipeStopResult()),
        )
    )
    with sessions() as session:
        stored = session.get(RecipeRun, run.owner_id)
        assert stored is not None and stored.state == RunState.STOPPED
        assert (
            session.scalar(
                select(ResourceReservation.id).where(
                    ResourceReservation.owner_id == run.owner_id,
                    ResourceReservation.state == ReservationState.ACTIVE,
                )
            )
            is None
        )
