"""Startup and reporting use the inventory owner without losing old samples."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_agent_protocol.telemetry import HostResourceSample, TelemetrySample
from vonk_control.db import initialize_database, node_facts
from vonk_control.models import Base, NodeInventoryResourceSnapshot, NodeTelemetrySample
from vonk_control.telemetry import TelemetryRepository

NODE = "spk_" + "c" * 32
NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


@pytest.mark.parametrize("empty", [False, True])
def test_resource_move_restarts_without_losing_or_inventing_measurements(
    postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    """Catches early drops, fake zeroes, lost equal-time samples and replay forks."""
    Base.metadata.create_all(postgres_engine)
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO agent_nodes (node_id, state) VALUES (%s, %s)",
            (NODE, NodeIdentityState.ACTIVE.value),
        )
        for name in HostResourceSample.model_fields:
            connection.exec_driver_sql(
                f'ALTER TABLE node_telemetry_samples ADD COLUMN "{name}" bigint'
            )
        if not empty:
            for index, values in enumerate(
                ((100, 60, 200, 80), (None, None, None, None), (100, 0, 200, 0)), 1
            ):
                identifier = f"00000000-0000-4000-8000-{index:012d}"
                connection.exec_driver_sql(
                    """INSERT INTO node_telemetry_samples
                    (id,node_id,boot_id,observed_at,received_at,memory_total_bytes,memory_available_bytes,disk_total_bytes,disk_free_bytes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (identifier, NODE, identifier, NOW, NOW, *values),
                )

    move = node_facts.move_resource_facts

    def interrupted(connection: Connection) -> None:
        def fail(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith("ALTER TABLE node_telemetry_samples DROP COLUMN"):
                raise InterruptedError(
                    "Controller died after copy, before retiring scalar owner"
                )

        event.listen(connection, "before_cursor_execute", fail)
        try:
            move(connection)
        finally:
            event.remove(connection, "before_cursor_execute", fail)

    url = postgres_engine.url.render_as_string(hide_password=False)
    monkeypatch.setattr(node_facts, "move_resource_facts", interrupted)
    with pytest.raises(InterruptedError):
        initialize_database(url)
    with postgres_engine.connect() as connection:
        assert connection.scalar(select(NodeInventoryResourceSnapshot.id)) is None
        assert "memory_total_bytes" in {
            column["name"]
            for column in inspect(connection).get_columns("node_telemetry_samples")
        }
    monkeypatch.setattr(node_facts, "move_resource_facts", move)
    initialize_database(url)
    initialize_database(url)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions() as session:
        resources = list(
            session.scalars(
                select(NodeInventoryResourceSnapshot).order_by(
                    NodeInventoryResourceSnapshot.source_sample_id
                )
            )
        )
        assert len(resources) == (0 if empty else 3)
        if not empty:
            assert [
                row.host_resources.memory_available_bytes
                for row in resources
                if isinstance(row.host_resources, HostResourceSample)
            ] == [60, None, 0]
        assert not set(HostResourceSample.model_fields) & set(
            NodeTelemetrySample.__table__.columns.keys()
        )
        assert not set(HostResourceSample.model_fields) & {
            column["name"]
            for column in inspect(session.connection()).get_columns(
                "node_telemetry_samples"
            )
        }
    if empty:
        return
    repository = TelemetryRepository(sessions, clock=lambda: NOW)
    sample = TelemetrySample(
        boot_id="00000000-0000-4000-8000-000000000003",
        observed_at=NOW,
        memory_total_bytes=100,
        memory_available_bytes=0,
        disk_total_bytes=200,
        disk_free_bytes=0,
        gpu_utilization_percent=None,
        gpu_memory_total_bytes=None,
        gpu_memory_free_bytes=None,
    )
    accepted = repository.record_batch(NODE, [sample])
    assert accepted[0].memory_available_bytes == 0
    with sessions() as session:
        assert len(list(session.scalars(select(NodeInventoryResourceSnapshot)))) == 3


def test_resource_reporting_rolls_back_partial_sample_and_retries(
    postgres_engine: Engine,
) -> None:
    """Catches a telemetry commit whose resource owner was not committed with it."""
    from datetime import timedelta

    from vonk_control.models import AgentNode, NodeTelemetryLatest
    from vonk_control.telemetry_maintenance import TelemetryMaintenance

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE, state=NodeIdentityState.ACTIVE))
    sample = TelemetrySample(
        boot_id="00000000-0000-4000-8000-000000000001",
        observed_at=NOW,
        memory_total_bytes=100,
        memory_available_bytes=0,
        disk_total_bytes=None,
        disk_free_bytes=None,
        gpu_utilization_percent=None,
        gpu_memory_total_bytes=None,
        gpu_memory_free_bytes=None,
    )
    repository = TelemetryRepository(sessions, clock=lambda: NOW)

    def fail(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO node_inventory_snapshots"):
            raise InterruptedError(
                "Controller stopped after sample, before its inventory measurements"
            )

    event.listen(postgres_engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(InterruptedError):
            repository.record_batch(NODE, [sample])
    finally:
        event.remove(postgres_engine, "before_cursor_execute", fail)
    with sessions() as session:
        assert session.scalar(select(NodeTelemetrySample.id)) is None
        assert session.get(NodeTelemetryLatest, NODE) is None
    repository.record_batch(NODE, [sample])
    repository.record_batch(NODE, [sample])
    assert repository.latest([NODE])[NODE].memory_available_bytes == 0
    # The existing telemetry lifetime owns collection of the exact linked
    # resource record too; there is no independent stale archive scheduler.
    TelemetryMaintenance(sessions, clock=lambda: NOW + timedelta(hours=25)).run_once()
    with sessions() as session:
        assert session.scalar(select(NodeTelemetrySample.id)) is None
        assert session.scalar(select(NodeInventoryResourceSnapshot.id)) is None


def test_archived_measurement_lock_does_not_block_install_admission(
    postgres_engine: Engine, tmp_path
) -> None:
    """Catches locking telemetry history as if it were current admission evidence."""
    from vonk_control.models import RecipeInstallation

    from .test_recipe_operations import NOW as now
    from .test_recipe_operations import setup_services

    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    node_id = nodes[0]
    service = lifecycle._install_admission
    plan = service.plan_install(mapping_id, build_id, now=now)
    assert plan.allowed
    sample = TelemetrySample(
        boot_id="00000000-0000-4000-8000-000000000001",
        observed_at=now,
        memory_total_bytes=None,
        memory_available_bytes=None,
        disk_total_bytes=None,
        disk_free_bytes=None,
        gpu_utilization_percent=None,
        gpu_memory_total_bytes=None,
        gpu_memory_free_bytes=None,
    )
    TelemetryRepository(sessions, clock=lambda: now).record_batch(node_id, [sample])
    with sessions.begin() as history:
        locked = history.scalar(
            select(NodeInventoryResourceSnapshot)
            .where(NodeInventoryResourceSnapshot.node_id == node_id)
            .with_for_update(of=NodeInventoryResourceSnapshot)
        )
        assert locked is not None
        identifier = service.accept_install(plan, actor="admin", now=now)
    with sessions() as session:
        assert session.get(RecipeInstallation, identifier) is not None
