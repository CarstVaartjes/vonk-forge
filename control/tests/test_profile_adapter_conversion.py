"""Retained profile journals are converted once, without changing live effects."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import Engine, Table, select, update
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    OutcomeDone,
    OutcomeKind,
    RecipeStopResult,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.db import initialize_database
from vonk_control.fleet_profile_adapter_conversion import (
    conversion_progress,
    convert_due_retained_applications,
    needs_conversion,
    try_convert_application,
)
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    _persisted_profile_progress,
    build_production_fleet_profile_service,
)
from vonk_control.models import (
    AgentOperation,
    FleetProfileApplication,
    Job,
    ResourceReservation,
)

from .agent_fences import fenced_operation
from .profile_stop_readmission_support import start_on_released_gang
from .test_fleet_profiles import NOW, _uuid
from .test_profile_continuity_postgres import (
    _assessment_with_promises,
    _SQLCancellationAdapter,
)
from .test_recipe_operations import (
    _issue_exact_stop_grant,
    installed_recipe,
    setup_services,
    started_recipe,
)
from .test_run_switch_operations import RecordingArtifactExecutor, _service


def _persist_retained(session, row, progress):
    """Seed bytes retained from the retired producer, without an active old writer."""
    table = FleetProfileApplication.__table__
    assert isinstance(table, Table)
    session.execute(update(table).where(table.c.id == row.id).values(progress=progress))
    session.expire(row, ["progress"])


def _retained_stop(tmp_path, engine: Engine | None = None):
    sessions, lifecycle, _queue, mapping, build, nodes = setup_services(
        tmp_path, engine=engine
    )
    installation = installed_recipe(
        lifecycle, mapping, build, nodes, request_id=_uuid(18800)
    )
    run = started_recipe(
        sessions, lifecycle, installation.owner_id, nodes, request_id=_uuid(18801)
    )
    lifecycle._clock = lambda: NOW
    profiles = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=_SQLCancellationAdapter(),
        assessment_provider=_assessment_with_promises,
    )
    profile = profiles.create(
        FleetProfileInput(name="Idle while retaining exact cleanup", assignments=[]),
        actor="admin",
    )
    application = profiles.apply(profile.id, request_key=_uuid(18802), actor="admin")
    switches = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    adapter = RunSwitchFleetProfileAdapter(sessions, switches)
    # Persist the accepted running profile step through its actual worker.
    # Direct adapter startup would leave the application queued and cannot
    # authorize its destructive Stop after retained bookkeeping conversion.
    profiles._switch_adapter = adapter
    profiles.tick()
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        current = _persisted_profile_progress(row).switch_adapter
        assert current is not None and len(current.pending_children) == 1
        pending = current.pending_children[0]
        old = current.model_dump(mode="json")
        old.pop("pending_children")
        old.pop("skipped_indices")
        old["active_operation_id"] = pending.operation_id
        old["active_kind"] = pending.kind
        old["position"] = pending.queue_index
        for child in old["children"]:
            child.pop("queue_index")
            child.pop("original_operation_id")
        progress = deepcopy(row.progress)
        progress["switch_adapter"] = old
        _persist_retained(session, row, progress)
        original = deepcopy(row.progress)
        stop_id = pending.operation_id
        claims = {
            claim.id
            for claim in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == application.id
                )
            )
        }
    return sessions, lifecycle, switches, application, run, stop_id, original, claims


def test_retained_exact_stop_conversion_reconnects_without_another_request(tmp_path):
    """Catches turning an upgrade checkpoint into another Stop or application."""
    sessions, _lifecycle, switches, application, _run, stop_id, original, claims = (
        _retained_stop(tmp_path)
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None and needs_conversion(row)
        assert try_convert_application(session, row, NOW).state == "converted"
        converted = deepcopy(row.progress)
        current = _persisted_profile_progress(row).switch_adapter
        assert current is not None
        original_adapter = original["switch_adapter"]
        assert isinstance(original_adapter, dict)
        assert [
            (child.queue_index, child.operation_id, child.kind)
            for child in current.pending_children
        ] == [(original_adapter["position"], stop_id, "stop")]
        assert try_convert_application(session, row, NOW).state == "current"
        assert row.progress == converted
    restarted = RunSwitchFleetProfileAdapter(sessions, switches)
    observed = restarted.get(application.id)
    assert observed.id == application.id
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        assert row.progress == converted
        child = session.get(Job, stop_id)
        assert child is not None
        assert (
            session.scalar(select(Job.id).where(Job.request_id == child.request_id))
            == stop_id
        )
        assert {
            claim.id
            for claim in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == application.id
                )
            )
        } == claims


def test_retained_unknown_projection_preserves_outer_metadata_and_original_journal(
    tmp_path,
):
    """Catches destructive old-field projection or invented metadata after damage."""
    sessions, _lifecycle, _switches, application, _run, _stop_id, original, _claims = (
        _retained_stop(tmp_path)
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        projected = conversion_progress(row)
        assert projected is not None
        assert projected.attempt == original["attempt"]
        assert projected.completed_steps == original["completed_steps"]
        assert projected.intended_profile is not None
        assert projected.switch_adapter is None
        assert row.progress == original and needs_conversion(row)
        malformed = deepcopy(original)
        malformed["attempt"] = "unreadable"
        _persist_retained(session, row, malformed)
        assert conversion_progress(row) is None
        assert row.progress == malformed and needs_conversion(row)


def test_retained_closed_stop_maps_by_accepted_request_and_preserves_receipt(tmp_path):
    """Catches assigning terminal receipts by list order or losing their result."""
    sessions, lifecycle, switches, app, run, stop_id, _original, _claims = (
        _retained_stop(tmp_path)
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, app.id)
        assert row is not None
        assert try_convert_application(session, row, NOW).state == "converted"
    adapter = RunSwitchFleetProfileAdapter(sessions, switches)
    profiles = build_production_fleet_profile_service(
        sessions, clock=lambda: NOW, run_switch_operations=switches
    )
    jobs = AgentJobService(
        sessions, clock=lambda: NOW, result_consumer=lifecycle.consume_agent_result
    )
    completed = set()
    for _ in range(12):
        switches.tick()
        with sessions() as session:
            effects = tuple(
                session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.kind == WireAgentOperation.RECIPE_STOP.value
                    )
                )
            )
        for effect in effects:
            if effect.id not in completed:
                claim, _plan, _grant = _issue_exact_stop_grant(
                    sessions,
                    node_id=effect.node_id,
                    certificate_serial="serial-0",
                    grant_now=NOW,
                )
                assert fenced_operation(sessions, claim).id == effect.id
                jobs.record_result(
                    AgentResult(
                        fence=claim.fence,
                        state=AgentResultState.SUCCEEDED,
                        result=OutcomeDone(
                            kind=OutcomeKind.DONE, result=RecipeStopResult()
                        ),
                    )
                )
                completed.add(effect.id)
        adapter.advance(app.id)
        profiles.tick()
        with sessions() as session:
            row = session.get(FleetProfileApplication, app.id)
            assert row is not None
            state = _persisted_profile_progress(row).switch_adapter
            assert state is not None
        if state.result is not None:
            break
    assert state.result is not None and len(state.children) == 1
    receipt = state.children[0]
    assert receipt.operation_id == stop_id and receipt.state == "succeeded"
    old = state.model_dump(mode="json")
    old.pop("pending_children")
    old.pop("skipped_indices")
    old.update(active_operation_id=None, active_kind=None)
    for child in old["children"]:
        child.pop("queue_index")
        child.pop("original_operation_id")
    for child in old["result"]["children"]:
        child.pop("queue_index")
        child.pop("original_operation_id")
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, app.id)
        assert row is not None
        progress = deepcopy(row.progress)
        progress["switch_adapter"] = old
        _persist_retained(session, row, progress)
        assert try_convert_application(session, row, NOW).state == "converted"
        current = _persisted_profile_progress(row).switch_adapter
        assert current is not None and current.children == [receipt]
        assert current.result == state.result
    start_on_released_gang(
        sessions,
        lifecycle,
        run.owner_id,
        lifecycle.get(stop_id).nodes,
        clock=lambda: NOW,
        request_id=_uuid(18805),
    )


@pytest.mark.parametrize(
    "damage",
    [
        "wrong-request",
        "wrong-kind",
        "missing-child",
        "mixed-encoding",
        "unproven-closed-index",
        "wrong-intent",
        "malformed-child-payload",
        "foreign-targets",
        "foreign-closed-receipt",
    ],
)
def test_unproven_retained_stop_retires_without_reissuing_children(tmp_path, damage):
    """Catches inventing proof or indefinitely retrying an unprovable journal."""
    sessions, _lifecycle, _switches, application, _run, stop_id, _original, claims = (
        _retained_stop(tmp_path)
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        job = session.get(Job, stop_id)
        assert row is not None and job is not None
        if damage == "wrong-request":
            job.request_id = _uuid(18820)
        elif damage == "wrong-kind":
            job.kind = "recipe.run-switch.v2"
        elif damage == "missing-child":
            session.delete(job)
        elif damage == "wrong-intent":
            from vonk_control.run_switch_operations import _digest

            payload = deepcopy(job.payload)
            ordinal = payload["workload_intent_ordinal"]
            assert isinstance(ordinal, int)
            payload["workload_intent_ordinal"] = ordinal + 1
            job.payload = payload
            job.payload_digest = _digest(payload)
        elif damage == "malformed-child-payload":
            table = Job.__table__
            assert isinstance(table, Table)
            session.execute(
                update(table).where(table.c.id == job.id).values(payload=["damaged"])
            )
            session.expire(job, ["payload"])
        elif damage == "foreign-targets":
            job.targets = ["spk_" + "2" * 32]
        else:
            progress = deepcopy(row.progress)
            retained_adapter = progress["switch_adapter"]
            assert isinstance(retained_adapter, dict)
            if damage == "mixed-encoding":
                retained_adapter["pending_children"] = []
            else:
                retained_adapter["position"] = 1
                retained_adapter["active_operation_id"] = None
                retained_adapter["active_kind"] = None
                if damage == "foreign-closed-receipt":
                    foreign_id = session.scalar(select(Job.id).where(Job.id != stop_id))
                    assert foreign_id is not None
                    retained_adapter["children"] = [
                        {
                            "operation_id": foreign_id,
                            "kind": "stop",
                            "state": "succeeded",
                            "result": None,
                        }
                    ]
            _persist_retained(session, row, progress)
        session.flush()
        # The actual worker admitted this owner after apply returned its queued
        # view. Conversion must preserve the current persisted owner, not that
        # earlier response snapshot.
        assert row.id == application.id and row.state == "running"
        result = try_convert_application(session, row, NOW)
        assert result.state == "converted"
        assert row.state == "cancelled"
        assert not needs_conversion(row)
        assert _persisted_profile_progress(row).switch_adapter is None
        assert row.request_key == application.request_key
        assert row.plan_digest == application.plan_digest
        assert {
            claim.id
            for claim in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == application.id
                )
            )
        } == claims
        assert try_convert_application(session, row, NOW).state == "current"
    profiles = build_production_fleet_profile_service(
        sessions, clock=lambda: NOW, run_switch_operations=_switches
    )
    fresh = profiles.apply(
        application.profile_id, request_key=_uuid(18892), actor="admin"
    )
    assert fresh.id != application.id
    assert fresh.state in {"queued", "running", "succeeded"}


def test_postgres_startup_converts_real_stop_and_continuation_is_idempotent(
    postgres_engine, tmp_path
):
    """Catches omitted startup hookup and duplicate dispatch after SQL restart."""
    sessions, _lifecycle, switches, application, _run, stop_id, _original, claims = (
        _retained_stop(tmp_path, postgres_engine)
    )
    from pathlib import Path

    initialize_database(
        postgres_engine.url.render_as_string(hide_password=False),
        config_path=Path(__file__).resolve().parents[1] / "alembic.ini",
    )
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None and not needs_conversion(row)
        converted = deepcopy(row.progress)
    assert convert_due_retained_applications(sessions, NOW + timedelta(seconds=1)) == 0
    restarted = RunSwitchFleetProfileAdapter(sessions, switches)
    restarted.get(application.id)
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None and row.progress == converted
        assert session.get(Job, stop_id) is not None
        assert {
            claim.id
            for claim in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == application.id
                )
            )
        } == claims


@pytest.mark.parametrize(
    "damage", ["invalid-contract", "invalid-outer-contract", "different-effect"]
)
def test_postgres_damaged_journal_retires_and_fresh_same_target_proceeds(
    postgres_engine, tmp_path, damage
):
    """Catches perpetual conversion deferral blocking newer same-target intent."""
    sessions, _lifecycle, switches, application, _run, stop_id, original, _claims = (
        _retained_stop(tmp_path, postgres_engine)
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        damaged = deepcopy(original)
        adapter = damaged["switch_adapter"]
        assert isinstance(adapter, dict)
        if damage == "invalid-contract":
            adapter["position"] = "damaged"
        elif damage == "invalid-outer-contract":
            damaged["attempt"] = "damaged"
        else:
            queue = adapter["queue"]
            assert isinstance(queue, list)
            queue[0]["id"] = _uuid(18890)
            # Keep the child's request binding exact, but change its accepted
            # run identity so proof fails on the effect, not on the request key.
            from vonk_control.fleet_profile_contract import (
                profile_switch_child_request_key,
            )

            child = session.get(Job, stop_id)
            assert child is not None
            child.request_id = profile_switch_child_request_key(
                row.id, 0, "stop", _uuid(18890)
            )
        _persist_retained(session, row, damaged)
        # A deployment may retain a deferral from the previous converter. Even
        # its future retry timestamp must not postpone retirement on this pass.
        row.status_reason = "Profile journal conversion waiting: retrying automatically"
        row.updated_at = NOW + timedelta(seconds=30)
    convert_due_retained_applications(sessions, NOW)
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        assert row.state == "cancelled"
        assert not needs_conversion(row)
        assert session.get(Job, stop_id) is not None
    profiles = build_production_fleet_profile_service(
        sessions, clock=lambda: NOW, run_switch_operations=switches
    )
    fresh = profiles.apply(
        application.profile_id, request_key=_uuid(18891), actor="admin"
    )
    assert fresh.id != application.id
    assert fresh.state in {"queued", "running", "succeeded"}
    profiles.tick()
    with sessions() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None and row.state == "cancelled"
        assert not needs_conversion(row)
