import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from vonk_control import fleet_events as fleet_event_module
from vonk_control import models
from vonk_control.db import session_factory
from vonk_control.fleet_events import (
    FleetEventDraft,
    FleetEventRecorder,
    FleetEventRepository,
)
from vonk_control.telemetry import (
    TelemetryDetailsInput,
    TelemetryRepository,
    TelemetrySampleInput,
)

NOW = datetime(2026, 8, 15, 12, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fleet-events.sqlite'}")
    models.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    return factory


def _draft(
    *,
    event_type: str = "operation-state",
    entity_id: str = "job-1",
    payload: dict[str, object] | None = None,
) -> FleetEventDraft:
    return FleetEventDraft(
        event_type=event_type,
        node_id=None,
        entity_kind="job",
        entity_id=entity_id,
        payload=payload
        or {
            "schema_version": 1,
            "entity_kind": "job",
            "entity_id": entity_id,
            "kind": "deploy",
            "state": "queued",
            "target_count": 1,
        },
    )


def _job(identifier: str) -> models.Job:
    return models.Job(
        id=identifier,
        request_id=f"request-{identifier}",
        kind="deploy",
        state="queued",
        actor="operator@example.invalid",
        base_commit="a" * 40,
        targets=["spk_" + "a" * 32],
        payload_digest="b" * 64,
        payload={"credential": "must-never-enter-event-payload"},
        result=None,
        status_reason="private detail",
        current_attempt=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _installation(identifier: str = "installation-1") -> models.RecipeInstallation:
    return models.RecipeInstallation(
        id=identifier,
        recipe_revision_id="revision-1",
        mapping_id="mapping-1",
        mapping_generation=3,
        recipe_build_id="build-1",
        image_digest="sha256:" + "c" * 64,
        plan_digest="d" * 64,
        plan={"secret": "private install plan"},
        state="planned",
        actor="private actor",
        created_at=NOW,
        updated_at=NOW,
    )


def _installation_node() -> models.InstallationNode:
    return models.InstallationNode(
        id="installation-node-1",
        installation_id="installation-1",
        node_id="spk_" + "a" * 32,
        rank=0,
        role="leader",
        state="planned",
        required_bytes=1000,
        installed_bytes=0,
        evidence_digest="e" * 64,
        updated_at=NOW,
    )


def _run(identifier: str = "run-1", *, alias: str = "chat") -> models.RecipeRun:
    return models.RecipeRun(
        id=identifier,
        installation_id="installation-1",
        mapping_id="mapping-1",
        mapping_generation=3,
        alias=alias,
        plan_digest=("f" if identifier == "run-1" else "a") * 64,
        plan={"credential": "private run plan"},
        state="planned",
        route_state="withdrawn",
        route_generation=None,
        route_digest=None,
        route_error="private route error",
        actor="private actor",
        created_at=NOW,
        updated_at=NOW,
        stopped_at=None,
    )


def _run_node() -> models.RunNode:
    return models.RunNode(
        id="run-node-1",
        run_id="run-1",
        node_id="spk_" + "a" * 32,
        rank=0,
        role="leader",
        state="planned",
        port=8080,
        reserved_memory_bytes=2000,
        observed_memory_bytes=None,
        endpoint={"credential": "private endpoint"},
        evidence_digest="1" * 64,
        updated_at=NOW,
    )


def _operation() -> models.AgentOperation:
    return models.AgentOperation(
        id="operation-1",
        parent_job_id="job-1",
        node_id="spk_" + "a" * 32,
        kind="deploy",
        payload_digest="2" * 64,
        payload={"secret": "private operation payload"},
        base_commit="a" * 40,
        state="queued",
        current_attempt=0,
        retry_disposition=None,
        retry_disposition_attempt=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _event_rows(sessions) -> list[models.FleetStreamEvent]:
    with sessions() as session:
        return list(
            session.scalars(
                select(models.FleetStreamEvent).order_by(models.FleetStreamEvent.id)
            )
        )


def _telemetry_sample(
    *, boot_id: str, sequence: int, observed_at: datetime, cpu: float | None = None
) -> TelemetrySampleInput:
    return TelemetrySampleInput(
        boot_id=uuid.UUID(boot_id),
        sequence=sequence,
        observed_at=observed_at,
        cpu_utilization_percent=cpu,
        load_average_1m=None,
        memory_total_bytes=None,
        memory_available_bytes=None,
        disk_total_bytes=None,
        disk_free_bytes=None,
        gpu_utilization_percent=None,
        gpu_memory_total_bytes=None,
        gpu_memory_free_bytes=None,
        temperature_c=None,
        power_watts=None,
        network_receive_bytes_per_second=None,
        network_transmit_bytes_per_second=None,
        gap_samples=0,
        details=TelemetryDetailsInput(),
    )


def test_fleet_event_models_match_the_0024_schema_contract() -> None:
    cursor = models.FleetEventCursor.__table__
    events = models.FleetStreamEvent.__table__

    assert [column.name for column in cursor.columns] == ["singleton_id", "last_id"]
    assert {constraint.name for constraint in cursor.constraints} == {
        None,
        "ck_fleet_event_cursor_singleton",
        "ck_fleet_event_cursor_last_id",
    }
    assert [column.name for column in events.columns] == [
        "id",
        "event_type",
        "node_id",
        "entity_kind",
        "entity_id",
        "payload",
        "occurred_at",
        "expires_at",
    ]
    assert {constraint.name for constraint in events.constraints} == {
        None,
        "ck_fleet_stream_events_event_type",
        "ck_fleet_stream_events_expiry",
        "ck_fleet_stream_events_payload_size",
    }
    assert {(index.name, tuple(index.columns.keys())) for index in events.indexes} == {
        ("ix_fleet_stream_events_expires_id", ("expires_at", "id")),
        ("ix_fleet_stream_events_node_id", ("node_id", "id")),
    }


def test_repository_allocates_increasing_ids_and_exact_semantic_expiry(
    sessions,
) -> None:
    repository = FleetEventRepository(sessions, clock=lambda: NOW)

    with sessions.begin() as session:
        first = repository.append_in_session(session, _draft(entity_id="job-1"))
        second = repository.append_in_session(session, _draft(entity_id="job-2"))

    assert (first.id, second.id) == (1, 2)
    assert first.occurred_at == NOW
    assert first.expires_at == NOW + timedelta(hours=24)
    assert repository.high_watermark() == 2


def test_cursor_allocator_compiles_to_a_postgresql_row_lock() -> None:
    statement = fleet_event_module._cursor_lock_statement()
    compiled = str(statement.compile(dialect=postgresql.dialect()))

    assert "FROM fleet_event_cursor" in compiled
    assert "FOR UPDATE" in compiled


def test_repository_rollback_removes_source_event_and_cursor_advance(sessions) -> None:
    repository = FleetEventRepository(sessions, clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="forced rollback"), sessions.begin() as session:
        session.add(_job("job-rollback"))
        repository.append_in_session(session, _draft(entity_id="job-rollback"))
        raise RuntimeError("forced rollback")

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(models.Job)) == 0
        assert (
            session.scalar(select(func.count()).select_from(models.FleetStreamEvent))
            == 0
        )
        assert session.get(models.FleetEventCursor, 1).last_id == 0


@pytest.mark.parametrize(
    "draft",
    [
        _draft(event_type="fleet-snapshot"),
        _draft(payload={"schema_version": 1, "secret_token": "do-not-store"}),
        _draft(payload={"schema_version": 1, "value": "x" * 8192}),
        _draft(payload={"schema_version": 1, "value": object()}),
    ],
    ids=["event-vocabulary", "secret-field", "payload-size", "json-type"],
)
def test_invalid_draft_fails_the_source_transaction(sessions, draft) -> None:
    repository = FleetEventRepository(sessions, clock=lambda: NOW)

    with pytest.raises((TypeError, ValueError)), sessions.begin() as session:
        session.add(_job("job-invalid"))
        repository.append_in_session(session, draft)

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(models.Job)) == 0
        assert (
            session.scalar(select(func.count()).select_from(models.FleetStreamEvent))
            == 0
        )
        assert session.get(models.FleetEventCursor, 1).last_id == 0


def test_repository_reads_are_bounded_ordered_and_semantically_unexpired(
    sessions,
) -> None:
    times = iter((NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2)))
    repository = FleetEventRepository(sessions, clock=lambda: next(times))
    with sessions.begin() as session:
        repository.append_in_session(session, _draft(entity_id="job-1"))
        repository.append_in_session(session, _draft(entity_id="job-2"))
        repository.append_in_session(session, _draft(entity_id="job-3"))

    replay_at = NOW + timedelta(hours=24, minutes=30)
    window = repository.retention_window(replay_at)
    assert (window.high_watermark, window.first_retained_id) == (3, 2)
    assert [event.id for event in repository.after(0, replay_at, limit=1)] == [2]
    assert [event.id for event in repository.after(2, replay_at, limit=128)] == [3]
    with sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(models.FleetStreamEvent))
            == 3
        )


def test_retention_window_reads_one_database_snapshot(sessions) -> None:
    repository = FleetEventRepository(sessions, clock=lambda: NOW)
    statements: list[str] = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", observe)
    try:
        assert repository.retention_window(NOW).high_watermark == 0
    finally:
        event.remove(engine, "before_cursor_execute", observe)

    assert len(statements) == 1


@pytest.mark.parametrize("limit", [0, 129])
def test_repository_rejects_unbounded_read_limits(sessions, limit: int) -> None:
    repository = FleetEventRepository(sessions, clock=lambda: NOW)
    with pytest.raises(ValueError, match="limit"):
        repository.after(0, NOW, limit=limit)


def test_recorder_captures_every_authoritative_insert_with_public_payloads(
    sessions,
) -> None:
    FleetEventRecorder.install(sessions, clock=lambda: NOW)
    with sessions.begin() as session:
        session.add_all(
            [
                models.NodeTelemetryLatest(
                    node_id="spk_" + "a" * 32, sample_id="sample-1"
                ),
                _installation(),
                _installation_node(),
                _run(),
                _run_node(),
                _job("job-1"),
                _operation(),
            ]
        )

    rows = _event_rows(sessions)
    assert [row.id for row in rows] == list(range(1, 8))
    assert [row.event_type for row in rows] == [
        "node-telemetry",
        "recipe-state",
        "recipe-state",
        "recipe-state",
        "recipe-state",
        "operation-state",
        "operation-state",
    ]
    assert [row.entity_kind for row in rows] == [
        "node-telemetry-latest",
        "recipe-installation",
        "installation-node",
        "recipe-run",
        "run-node",
        "job",
        "agent-operation",
    ]
    assert rows[0].payload == {
        "schema_version": 1,
        "node_id": "spk_" + "a" * 32,
        "sample_id": "sample-1",
    }
    assert rows[5].payload == {
        "schema_version": 1,
        "entity_kind": "job",
        "entity_id": "job-1",
        "kind": "deploy",
        "state": "queued",
        "target_count": 1,
    }
    assert rows[6].payload == {
        "schema_version": 1,
        "entity_kind": "agent-operation",
        "entity_id": "operation-1",
        "parent_job_id": "job-1",
        "node_id": "spk_" + "a" * 32,
        "kind": "deploy",
        "state": "queued",
        "attempt": 0,
    }
    serialized = repr([row.payload for row in rows]).lower()
    for private_value in (
        "private actor",
        "private install plan",
        "private run plan",
        "private route error",
        "private endpoint",
        "private operation payload",
        "must-never-enter-event-payload",
        "private detail",
    ):
        assert private_value not in serialized
    assert all(
        len(
            json.dumps(
                row.payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        <= 8192
        for row in rows
    )


def test_recorder_orders_same_transaction_entities_deterministically(sessions) -> None:
    FleetEventRecorder.install(sessions, clock=lambda: NOW)
    with sessions.begin() as session:
        session.add_all([_job("job-b"), _job("job-a")])

    assert [(row.id, row.entity_id) for row in _event_rows(sessions)] == [
        (1, "job-a"),
        (2, "job-b"),
    ]


def test_recorder_uses_attribute_history_for_every_authoritative_transition(
    sessions,
) -> None:
    FleetEventRecorder.install(sessions, clock=lambda: NOW)
    with sessions.begin() as session:
        session.add_all(
            [
                models.NodeTelemetryLatest(
                    node_id="spk_" + "a" * 32, sample_id="sample-1"
                ),
                _installation(),
                _installation_node(),
                _run(),
                _run_node(),
                _job("job-1"),
                _operation(),
            ]
        )
    with sessions.begin() as session:
        session.get(models.NodeTelemetryLatest, "spk_" + "a" * 32).sample_id = (
            "sample-2"
        )
        session.get(models.RecipeInstallation, "installation-1").state = "installed"
        installation_node = session.get(models.InstallationNode, "installation-node-1")
        installation_node.state = "installed"
        installation_node.installed_bytes = 1000
        run = session.get(models.RecipeRun, "run-1")
        run.state = "running"
        run.route_state = "published"
        run_node = session.get(models.RunNode, "run-node-1")
        run_node.state = "running"
        run_node.observed_memory_bytes = 1500
        session.get(models.Job, "job-1").state = "running"
        operation = session.get(models.AgentOperation, "operation-1")
        operation.state = "running"
        operation.current_attempt = 1

    transitions = _event_rows(sessions)[7:]
    assert [row.id for row in transitions] == list(range(8, 15))
    assert [row.payload["state"] for row in transitions[1:]] == [
        "installed",
        "installed",
        "running",
        "running",
        "running",
        "running",
    ]
    assert transitions[0].payload["sample_id"] == "sample-2"
    assert transitions[2].payload["installed_bytes"] == 1000
    assert transitions[3].payload["route_state"] == "published"
    assert transitions[4].payload["observed_memory_bytes"] == 1500
    assert transitions[6].payload["attempt"] == 1


def test_recorder_emits_nothing_for_irrelevant_writes_or_exact_telemetry_replay(
    sessions,
) -> None:
    FleetEventRecorder.install(sessions, clock=lambda: NOW)
    with sessions.begin() as session:
        session.add_all(
            [
                models.NodeTelemetryLatest(
                    node_id="spk_" + "a" * 32, sample_id="sample-1"
                ),
                _installation(),
                _installation_node(),
                _run(),
                _run_node(),
                _job("job-1"),
                _operation(),
            ]
        )
    with sessions.begin() as session:
        session.get(models.NodeTelemetryLatest, "spk_" + "a" * 32).sample_id = (
            "sample-1"
        )
        session.get(models.RecipeInstallation, "installation-1").updated_at = (
            NOW + timedelta(seconds=1)
        )
        session.get(models.InstallationNode, "installation-node-1").updated_at = (
            NOW + timedelta(seconds=1)
        )
        session.get(models.RecipeRun, "run-1").updated_at = NOW + timedelta(seconds=1)
        session.get(models.RunNode, "run-node-1").updated_at = NOW + timedelta(
            seconds=1
        )
        session.get(models.Job, "job-1").updated_at = NOW + timedelta(seconds=1)
        session.get(models.AgentOperation, "operation-1").updated_at = (
            NOW + timedelta(seconds=1)
        )

    assert len(_event_rows(sessions)) == 7


def test_telemetry_repository_emits_only_when_latest_pointer_advances(sessions) -> None:
    node_id = "spk_" + "a" * 32
    with sessions.begin() as session:
        session.add(models.AgentNode(node_id=node_id, state="active", capabilities=[]))
    FleetEventRecorder.install(sessions, clock=lambda: NOW)
    telemetry = TelemetryRepository(sessions, clock=lambda: NOW)
    latest = _telemetry_sample(
        boot_id="00000000-0000-4000-8000-000000000001",
        sequence=1,
        observed_at=NOW - timedelta(seconds=1),
    )
    older_other_boot = _telemetry_sample(
        boot_id="00000000-0000-4000-8000-000000000002",
        sequence=0,
        observed_at=NOW - timedelta(seconds=2),
    )

    telemetry.record_batch(node_id, (latest,))
    telemetry.record_batch(node_id, (latest,))
    telemetry.record_batch(node_id, (older_other_boot,))
    with pytest.raises(ValueError, match="conflicts"):
        telemetry.record_batch(
            node_id,
            (
                _telemetry_sample(
                    boot_id="00000000-0000-4000-8000-000000000001",
                    sequence=1,
                    observed_at=NOW - timedelta(seconds=1),
                    cpu=50,
                ),
            ),
        )

    rows = _event_rows(sessions)
    assert [(row.event_type, row.payload["sample_id"]) for row in rows] == [
        ("node-telemetry", telemetry.latest((node_id,))[node_id].id)
    ]
    with sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(models.NodeTelemetrySample))
            == 2
        )


def test_recorder_source_flush_and_event_are_rolled_back_together(sessions) -> None:
    FleetEventRecorder.install(sessions, clock=lambda: NOW)
    with sessions.begin() as session:
        session.add(_job("job-1"))
    assert len(_event_rows(sessions)) == 1

    with pytest.raises(RuntimeError, match="forced rollback"), sessions.begin() as session:
        job = session.get(models.Job, "job-1")
        job.state = "running"
        session.flush()
        raise RuntimeError("forced rollback")

    with sessions() as session:
        assert session.get(models.Job, "job-1").state == "queued"
        assert session.get(models.FleetEventCursor, 1).last_id == 1
        assert (
            session.scalar(select(func.count()).select_from(models.FleetStreamEvent))
            == 1
        )


def test_recorder_installation_is_idempotent_and_uninstall_removes_listeners(
    sessions,
) -> None:
    first = FleetEventRecorder.install(sessions, clock=lambda: NOW)
    second = FleetEventRecorder.install(sessions, clock=lambda: NOW + timedelta(days=1))
    assert first is second

    with sessions.begin() as session:
        session.add(_job("job-1"))
    assert len(_event_rows(sessions)) == 1

    first.uninstall()
    with sessions.begin() as session:
        session.add(_job("job-2"))
    assert len(_event_rows(sessions)) == 1


def test_recorder_clears_failed_and_closed_session_state(sessions) -> None:
    FleetEventRecorder.install(sessions, clock=lambda: NOW)
    session = sessions()
    with pytest.raises(ValueError, match="8192"):
        session.add(_run("run-too-large", alias="x" * 8192))
        session.commit()
    session.rollback()
    session.add(_run("run-valid", alias="valid"))
    session.commit()
    session.close()

    abandoned = sessions()
    abandoned.add(_job("job-abandoned"))
    abandoned.flush()
    abandoned.close()
    with sessions.begin() as committed:
        committed.add(_job("job-committed"))

    rows = _event_rows(sessions)
    assert [(row.entity_kind, row.entity_id) for row in rows] == [
        ("recipe-run", "run-valid"),
        ("job", "job-committed"),
    ]
    with sessions() as check:
        assert check.get(models.Job, "job-abandoned") is None
        assert check.get(models.FleetEventCursor, 1).last_id == 2


def test_production_session_factory_installs_the_recorder(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'production-seam.sqlite'}")
    models.Base.metadata.create_all(engine)
    production_sessions = session_factory(engine)
    with production_sessions() as session:
        assert session.get(models.FleetEventCursor, 1).last_id == 0
    with production_sessions.begin() as session:
        session.add(_job("job-production"))

    rows = _event_rows(production_sessions)
    assert [(row.id, row.entity_kind, row.entity_id) for row in rows] == [
        (1, "job", "job-production")
    ]
