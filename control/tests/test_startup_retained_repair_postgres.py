"""Startup bookkeeping damage stays local and the same accepted Stop heals."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path

from sqlalchemy import Table, event, select, update
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    OutcomeDone,
    OutcomeKind,
    RecipeStopResult,
    StateAlias,
)
from vonk_control import db
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_profile_adapter_conversion import needs_conversion
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    FleetProfileService,
    RunSwitchFleetProfileAdapter,
    _persisted_profile_progress,
)
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    AgentOperation,
    FleetProfileApplication,
    Job,
    ResourceReservation,
)

from .test_fleet_profiles import NOW, _uuid
from .test_model_cache import _artifact, _download
from .test_profile_adapter_conversion import _persist_retained, _retained_stop
from .test_recipe_operations import _issue_exact_stop_grant


def _claim_snapshot(sessions):
    with sessions() as session:
        return {
            (row.id, row.owner_id, row.node_id, row.kind, row.state)
            for row in session.scalars(select(ResourceReservation))
        }


def test_actual_startup_adoption_fault_preserves_and_repairs_original_stop(
    postgres_engine, tmp_path: Path, monkeypatch, caplog
) -> None:
    """Catches startup rollback, global starvation and fabricated replacement intent."""
    sessions, lifecycle, switches, accepted, run, stop_id, original, _claims = (
        _retained_stop(tmp_path, postgres_engine)
    )
    # Establish current schema and expose the native Stop through the real
    # startup converter before planting damage. The later lock fault therefore
    # belongs to bookkeeping adoption, not an initial migration's DDL.
    db.initialize_database(
        postgres_engine.url.render_as_string(hide_password=False),
        config_path=Path(__file__).resolve().parents[1] / "alembic.ini",
    )
    with sessions() as session:
        row = session.get(FleetProfileApplication, accepted.id)
        assert row is not None and not needs_conversion(row)
        root_key, root_digest = row.request_key, row.plan_digest
    clock = [NOW]
    jobs = AgentJobService(sessions, clock=lambda: clock[0])
    lifecycle._agent_jobs = jobs
    lifecycle._clock = lambda: clock[0]
    jobs.set_result_consumer(lifecycle.consume_agent_result)
    switches._clock = lambda: clock[0]
    native_id = None
    for _ in range(8):
        switches.tick()
        with sessions() as session:
            native_id = session.scalar(
                select(AgentOperation.id).where(AgentOperation.kind == "recipe.stop")
            )
        if native_id is not None:
            break
    assert native_id is not None
    with sessions() as session:
        native = session.get(AgentOperation, native_id)
        child = session.get(Job, stop_id)
        assert native is not None and child is not None
        node_id, child_key = native.node_id, child.request_id
    old_claim, exact_stop, _grant = _issue_exact_stop_grant(
        sessions, node_id=node_id, certificate_serial="serial-0"
    )
    assert exact_stop.run_id == run.owner_id
    before_claims = _claim_snapshot(sessions)

    # An actual lost transport lease is reconciled by the native producer.
    clock[0] += timedelta(hours=1)
    jobs.reconcile_orders()
    with sessions.begin() as session:
        native = session.get(AgentOperation, native_id)
        row = session.get(FleetProfileApplication, accepted.id)
        assert native is not None and native.state != "running" and row is not None
        assert native.next_action_at is not None
        # These are retained released-producer bytes, not a second live writer.
        table = AgentOperation.__table__
        assert isinstance(table, Table)
        session.execute(
            update(table)
            .where(table.c.id == native_id)
            .values(state=StateAlias.WAITING_FOR_OPERATOR.value)
        )
        damaged = deepcopy(original)
        damaged["total_steps"] = "damaged retained count"
        _persist_retained(session, row, damaged)
        retained_plan = deepcopy(row.plan)

    # Hold the actual row that the final legacy-state pass must rewrite. The
    # test's connection factory only supplies a bounded lock timeout; startup,
    # adoption SQL, savepoint and schema verification remain production code.
    build_engine = db.build_engine
    startup_faults = []

    def bounded_engine(database_url):
        engine = build_engine(database_url)

        @event.listens_for(engine, "connect")
        def lock_budget(connection, _record):
            cursor = connection.cursor()
            cursor.execute("SET lock_timeout = '100ms'")
            cursor.close()

        @event.listens_for(engine, "handle_error")
        def remember_fault(context):
            if getattr(context.original_exception, "sqlstate", None) == "55P03":
                startup_faults.append(context.statement)

        return engine

    holder = sessions()
    assert holder.get(AgentOperation, native_id, with_for_update=True) is not None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(db, "build_engine", bounded_engine)
            db.initialize_database(
                postgres_engine.url.render_as_string(hide_password=False),
                config_path=Path(__file__).resolve().parents[1] / "alembic.ini",
            )
        assert any(
            statement is not None and "UPDATE agent_operations" in statement
            for statement in startup_faults
        )
        assert "legacy_states.py" in caplog.text
        assert "Lifecycle bookkeeping adoption deferred" in caplog.text
        with postgres_engine.connect() as connection:
            db.verify_schema_is_current(connection)
            assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
        with sessions() as session:
            row = session.get(FleetProfileApplication, accepted.id)
            assert row is not None and needs_conversion(row)
            assert row.progress == damaged and row.plan == retained_plan
            assert (row.request_key, row.plan_digest) == (root_key, root_digest)
            assert row.status_reason and "conversion waiting" in row.status_reason
            due = row.updated_at + timedelta(seconds=31)

        # A genuine unrelated storage worker can still accept, claim, transfer
        # and verify bytes while this historical row remains damaged/locked.
        cache = ModelCacheService(
            sessions,
            tmp_path / "unrelated-cache",
            reserve_bytes=0,
            fixture_sources=True,
        )
        try:
            data = b"unrelated startup recovery bytes"
            artifact = _artifact(tmp_path, data, model_content_sha256="e" * 64)
            finished = _download(
                cache,
                [artifact],
                model_content_sha256="e" * 64,
                request_key=_uuid(19801),
            )
            assert finished.state == "succeeded", finished.last_error
            assert cache._object_path(str(artifact["sha256"])).read_bytes() == data
        finally:
            cache.close()
        assert _claim_snapshot(sessions) == before_claims
    finally:
        holder.rollback()
        holder.close()

    # Repair only the original damaged journal. A fresh service's normal tick
    # continues the bounded converter; no private per-row conversion is called.
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, accepted.id)
        assert row is not None
        _persist_retained(session, row, original)
    db.initialize_database(
        postgres_engine.url.render_as_string(hide_password=False),
        config_path=Path(__file__).resolve().parents[1] / "alembic.ini",
    )
    with sessions() as session:
        native = session.get(AgentOperation, native_id)
        assert native is not None
        assert native.state != StateAlias.WAITING_FOR_OPERATOR.value
    restarted = FleetProfileService(sessions, clock=lambda: due)
    restarted.tick()
    with sessions() as session:
        row = session.get(FleetProfileApplication, accepted.id)
        child = session.get(Job, stop_id)
        assert row is not None and child is not None and not needs_conversion(row)
        assert (row.request_key, row.plan_digest) == (root_key, root_digest)
        assert row.plan == retained_plan and child.request_id == child_key
        progress = _persisted_profile_progress(row)
        assert progress.switch_adapter is not None
        assert [
            item.operation_id for item in progress.switch_adapter.pending_children
        ] == [stop_id]
        assert (
            session.scalar(select(Job.id).where(Job.request_id == child_key)) == stop_id
        )
    assert _claim_snapshot(sessions) == before_claims

    # Resume the exact native Stop after startup, then prove its actual receipt
    # reaches the original accepted child. Retained aliases heal through the
    # ordinary native reconciler instead of a startup-only compatibility reader.
    fresh_jobs = AgentJobService(sessions, clock=lambda: clock[0])
    fresh_jobs.set_result_consumer(lifecycle.consume_agent_result)
    fresh_jobs.reconcile_orders()
    with sessions() as session:
        native = session.get(AgentOperation, native_id)
        assert native is not None
        if native.next_action_at is not None:
            clock[0] = max(clock[0], native.next_action_at)
    fresh_claim, fresh_stop, _fresh_grant = _issue_exact_stop_grant(
        sessions, node_id=node_id, certificate_serial="serial-0", grant_now=clock[0]
    )
    assert fresh_claim.payload == old_claim.payload
    assert fresh_stop.run_id == exact_stop.run_id
    receipt = AgentResult(
        fence=fresh_claim.fence,
        state=AgentResultState.SUCCEEDED,
        result=OutcomeDone(kind=OutcomeKind.DONE, result=RecipeStopResult()),
    )
    fresh_jobs.record_result(AgentResult.model_validate_json(receipt.model_dump_json()))
    adapter = RunSwitchFleetProfileAdapter(sessions, switches)
    for _ in range(4):
        switches.tick()
        adapter.advance(accepted.id)
    observed = adapter.get(accepted.id)
    assert observed.id == accepted.id
    with sessions() as session:
        row = session.get(FleetProfileApplication, accepted.id)
        assert row is not None
        progress = _persisted_profile_progress(row)
        assert progress.switch_adapter is not None
        assert any(
            item.operation_id == stop_id and item.state == "succeeded"
            for item in progress.switch_adapter.children
        )
        assert (row.request_key, row.plan_digest) == (root_key, root_digest)
        assert (
            session.scalar(select(Job.id).where(Job.request_id == child_key)) == stop_id
        )
    fresh_profiles = FleetProfileService(
        sessions,
        clock=lambda: due,
        switch_adapter=adapter,
        assessment_provider=adapter.assess,
    )
    followup = fresh_profiles.create(
        FleetProfileInput(name="Fresh authorized Idle"), actor="admin"
    )
    reviewed = fresh_profiles.preview(followup.id)
    assert reviewed.allowed, reviewed.reasons
    fresh = fresh_profiles.apply(followup.id, request_key=_uuid(19802), actor="admin")
    assert fresh.id != accepted.id and fresh.request_key == _uuid(19802)
    assert not fresh.progress.admission_pending
