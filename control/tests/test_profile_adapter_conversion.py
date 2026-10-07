"""Retained profile journals are converted once, without changing live effects."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import Engine, select
from vonk_control.db import initialize_database
from vonk_control.fleet_profile_adapter_conversion import (
    conversion_observation,
    convert_due_retained_applications,
    needs_conversion,
    try_convert_application,
)
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    _persisted_profile_progress,
)
from vonk_control.models import (
    AgentOperation,
    FleetProfileApplication,
    Job,
    ResourceReservation,
)

from .test_fleet_profiles import NOW, _uuid
from .test_profile_continuity_postgres import (
    _assessment_with_promises,
    _SQLCancellationAdapter,
)
from .test_recipe_operations import installed_recipe, setup_services, started_recipe
from .test_run_switch_operations import RecordingArtifactExecutor, _service


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
    adapter.start(
        application_id=application.id,
        assignments=(),
        scope_node_ids=nodes,
        actor="admin",
        request_id=_uuid(18803),
    )
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
        row.progress = progress
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
        assert [
            (child.queue_index, child.operation_id, child.kind)
            for child in current.pending_children
        ] == [(original["switch_adapter"]["position"], stop_id, "stop")]
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


def test_retained_closed_stop_maps_by_accepted_request_and_preserves_receipt(tmp_path):
    """Catches assigning terminal receipts by list order or losing their result."""
    sessions, lifecycle, switches, app, _run, stop_id, _original, _claims = (
        _retained_stop(tmp_path)
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, app.id)
        assert row is not None
        assert try_convert_application(session, row, NOW).state == "converted"
    adapter = RunSwitchFleetProfileAdapter(sessions, switches)
    completed = set()
    for _ in range(12):
        switches.tick()
        with sessions() as session:
            effects = tuple(
                session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.operation == "recipe.stop"
                    )
                )
            )
        for effect in effects:
            if effect.id not in completed:
                lifecycle.record_node_result(
                    effect.parent_job_id, effect.node_id, succeeded=True, evidence={}
                )
                completed.add(effect.id)
        adapter.advance(app.id)
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
        row.progress = progress
        assert try_convert_application(session, row, NOW).state == "converted"
        current = _persisted_profile_progress(row).switch_adapter
        assert current is not None and current.children == [receipt]
        assert current.result == state.result


@pytest.mark.parametrize(
    "damage",
    [
        "wrong-request",
        "wrong-kind",
        "missing-child",
        "mixed-encoding",
        "unproven-closed-index",
        "wrong-intent",
    ],
)
def test_ambiguous_retained_stop_preserves_raw_evidence_and_automatically_retries(
    tmp_path, damage
):
    """Catches inventing a queue binding, accepting a stale fence or retiring evidence."""
    sessions, _lifecycle, _switches, application, _run, stop_id, original, _claims = (
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
            payload["workload_intent_ordinal"] += 1
            job.payload = payload
            job.payload_digest = _digest(payload)
        else:
            progress = deepcopy(row.progress)
            if damage == "mixed-encoding":
                progress["switch_adapter"]["pending_children"] = []
            else:
                progress["switch_adapter"]["position"] = 1
                progress["switch_adapter"]["active_operation_id"] = None
                progress["switch_adapter"]["active_kind"] = None
            row.progress = progress
            original = deepcopy(progress)
        session.flush()
        result = try_convert_application(session, row, NOW)
        assert result.state == "deferred"
        assert result.next_attempt_at is not None and result.next_attempt_at > NOW
        assert row.progress == original
        assert row.id == application.id and row.state == application.state
        assert needs_conversion(row)
        assert conversion_observation(row) == result


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
