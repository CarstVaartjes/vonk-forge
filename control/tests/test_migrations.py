from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import (
    ForeignKeyConstraint,
    create_engine,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.telemetry import HostResourceSample
from vonk_control.db import initialize_database, verify_schema_is_current
from vonk_control.db.schema_helpers import SchemaDriftKind
from vonk_control.models import (
    AgentNode,
    Base,
    Job,
    NodeInventoryResourceSnapshot,
    NodeTelemetrySample,
    RoutePublicationOwner,
)

from .telemetry_support import telemetry_with_resources

ROOT = Path(__file__).resolve().parents[1]
LEGACY_TABLES = {
    "agent_upgrade_compatibility_recoveries",
    "local_recipe_revisions",
    "managed_recipe_library_links",
}


def test_startup_retires_unproven_distribution_identity_and_allows_redistribution(
    postgres_engine,
) -> None:
    """NULL identity must not block tightening or poison the next exact grant."""
    from uuid import uuid4

    from vonk_control.distribution import DistributionService, MemoryObjectSource
    from vonk_control.models import AgentNode, ArtifactDistributionAssignment

    from .test_distribution import _assignment

    database_url = postgres_engine.url.render_as_string(hide_password=False)
    _initialize_source_checkout(database_url)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    source = MemoryObjectSource()
    model_digest = source.put(b"model payload")
    assignment = _assignment(
        "spk_" + "a" * 32,
        model_digest,
        source.put(b"config!"),
        source.put(b"oci archive"),
    )
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(
        assignment.oci_image_digest, assignment.oci_archive_sha256
    )
    preserved = assignment.model_copy(
        update={"assignment_id": str(uuid4()), "plan_digest": "c" * 64}
    )
    with sessions.begin() as session:
        session.add(AgentNode(node_id=assignment.node_id, state="active"))
    service = DistributionService(source, sessions=sessions)
    service.register(assignment)
    service.register(preserved)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE artifact_distribution_assignments "
                "ALTER COLUMN oci_image_config_digest DROP NOT NULL"
            )
        )
        connection.execute(
            update(ArtifactDistributionAssignment)
            .where(ArtifactDistributionAssignment.id == assignment.assignment_id)
            .values(oci_image_config_digest=None)
        )

    _initialize_source_checkout(database_url)
    with postgres_engine.connect() as connection:
        column = next(
            column
            for column in inspect(connection).get_columns(
                ArtifactDistributionAssignment.__tablename__
            )
            if column["name"] == "oci_image_config_digest"
        )
        assert column["nullable"] is False
    with sessions() as session:
        assert (
            session.get(ArtifactDistributionAssignment, assignment.assignment_id)
            is None
        )

    # A new service cannot use process-local authorization from before startup.
    restarted = DistributionService(source, sessions=sessions)
    assert (
        restarted.authorize(
            node_id=preserved.node_id, plan_digest=preserved.plan_digest
        )
        == preserved
    )
    fresh = assignment.model_copy(
        update={"assignment_id": str(uuid4()), "generation": 2}
    )
    restarted.register(fresh)
    grant, _spec, opened = restarted.open_object(
        node_id=fresh.node_id, plan_digest=fresh.plan_digest, digest=model_digest
    )
    assert grant == fresh
    with opened.stream as stream:
        assert stream.read() == b"model payload"
    _initialize_source_checkout(database_url)
    assert (
        DistributionService(source, sessions=sessions).authorize(
            node_id=fresh.node_id, plan_digest=fresh.plan_digest
        )
        == fresh
    )


def _config(database_url: str) -> Config:
    config = Config(ROOT / "alembic.ini")
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _upgrade(database_url: str) -> None:
    command.upgrade(_config(database_url), "head")


def _initialize_source_checkout(database_url: str) -> None:
    initialize_database(database_url, config_path=ROOT / "alembic.ini")


def _schema_tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def _assert_current_schema(engine) -> None:
    assert _schema_tables(engine) == set(Base.metadata.tables)
    assert not LEGACY_TABLES & _schema_tables(engine)
    with engine.connect() as connection:
        assert list(MigrationContext.configure(connection).get_current_heads()) == [
            "0000_fresh_schema"
        ]
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0000_fresh_schema"
        )
        from alembic.autogenerate import compare_metadata

        differences = compare_metadata(
            MigrationContext.configure(connection), Base.metadata
        )
        if differences:
            assert len(differences) == 1
            operation, constraint = differences[0]
            assert operation == "add_constraint"
            assert constraint.name == "uq_model_cache_set_artifact_key"


def _assert_model_cache_operation_kind_is_current(engine) -> None:
    check = next(
        check
        for check in inspect(engine).get_check_constraints("model_cache_operations")
        if check["name"] == "ck_model_cache_operations_kind"
    )
    expression = (check["sqltext"] or "").lower()
    assert "download" in expression
    assert "repair" in expression
    assert "remove" in expression
    assert "evict" not in expression


def _assert_json_roundtrip(engine) -> None:
    payload = {
        "array": [1, None, False],
        "empty": {},
        "zero": 0,
        "text": "preserve this string",
    }
    request_id = "11111111-1111-4111-8111-111111111111"
    job_id = "22222222-2222-4222-8222-222222222222"
    now = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    job = Job(
        id=job_id,
        request_id=request_id,
        kind="cache.download",
        state="queued",
        actor="migration-test",
        authority_revision="authority-current",
        targets=["spark-a", "spark-b"],
        payload_digest=hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        payload=payload,
        result=None,
        current_attempt=0,
        created_at=now,
        updated_at=now,
    )
    with Session(engine) as session:
        session.add(job)
        session.commit()
    with Session(engine) as session:
        stored = session.get(Job, job_id)
        assert stored is not None
        assert stored.payload == payload
        assert stored.targets == ["spark-a", "spark-b"]
        assert stored.result is None
        assert stored.payload["array"] == [1, None, False]
        assert stored.payload["zero"] == 0


def test_fresh_sqlite_schema_is_exactly_current_metadata_and_roundtrips_json(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'fresh-control.sqlite'}"
    _upgrade(url)
    engine = create_engine(url)
    try:
        _assert_current_schema(engine)
        _assert_json_roundtrip(engine)
    finally:
        engine.dispose()


def test_fresh_baseline_adds_current_tables_to_existing_database(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'incompatible-control.sqlite'}"
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE agent_upgrade_compatibility_recoveries "
                    "(id VARCHAR(36) PRIMARY KEY)"
                )
            )
        _upgrade(url)
        assert "agent_upgrade_compatibility_recoveries" in _schema_tables(engine)
        assert set(Base.metadata.tables) <= _schema_tables(engine)
    finally:
        engine.dispose()


def test_only_one_fresh_baseline_revision_is_active() -> None:
    versions = ROOT / "migrations/versions"
    assert sorted(path.name for path in versions.glob("*.py")) == [
        "0000_fresh_schema.py"
    ]
    scripts = ScriptDirectory.from_config(_config("sqlite://"))
    assert scripts.get_heads() == ["0000_fresh_schema"]
    script = scripts.get_revision("0000_fresh_schema")
    assert script is not None
    assert script.down_revision is None


def test_baseline_has_no_parallel_schema_definition() -> None:
    migration = (ROOT / "migrations/versions/0000_fresh_schema.py").read_text()
    assert "Base.metadata.create_all" in migration
    assert "op.create_table" not in migration
    assert "sa.Table" not in migration
    assert "agent_upgrade_compatibility_recoveries" not in migration


def test_postgres_fresh_schema_matches_metadata_has_current_kind_and_roundtrips_json(
    postgres_engine,
) -> None:
    _upgrade(postgres_engine.url.render_as_string(hide_password=False))
    _assert_current_schema(postgres_engine)
    _assert_model_cache_operation_kind_is_current(postgres_engine)
    _assert_json_roundtrip(postgres_engine)
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    _initialize_source_checkout(database_url)
    _initialize_source_checkout(database_url)
    with postgres_engine.begin() as connection:
        event_id = connection.execute(
            text(
                "INSERT INTO fleet_stream_events "
                "(event_type, entity_kind, entity_id, payload, occurred_at, expires_at) "
                "VALUES ('node-profile', 'node', 'node-a', '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '1 hour') "
                "RETURNING id"
            )
        ).scalar_one()
        assert event_id > 0
        owner_id = connection.execute(
            insert(RoutePublicationOwner)
            .values(owner_generation=0)
            .returning(RoutePublicationOwner.singleton_id)
        ).scalar_one()
        assert owner_id == 1


def test_postgres_reconciliation_preserves_unknown_table_and_column_data(
    postgres_engine,
) -> None:
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    _upgrade(database_url)
    with postgres_engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE operator_extension (id integer PRIMARY KEY, value text)")
        )
        connection.execute(
            text("INSERT INTO operator_extension VALUES (7, 'retained')")
        )
        connection.execute(
            text(
                "INSERT INTO fleet_stream_events "
                "(event_type, entity_kind, entity_id, payload, occurred_at, expires_at) "
                "VALUES ('node-profile', 'node', 'node-a', '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '1 hour')"
            )
        )
        connection.execute(
            text("ALTER TABLE fleet_stream_events ADD COLUMN operator_note text")
        )
        connection.execute(
            text("UPDATE fleet_stream_events SET operator_note='retained'")
        )
        connection.execute(
            text(
                "ALTER TABLE fleet_stream_events ALTER COLUMN operator_note SET NOT NULL"
            )
        )
    _initialize_source_checkout(database_url)
    with postgres_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT value FROM operator_extension WHERE id=7")
            ).scalar_one()
            == "retained"
        )
        assert (
            connection.execute(
                text("SELECT operator_note FROM fleet_stream_events")
            ).scalar_one()
            == "retained"
        )
        nullable = next(
            column
            for column in inspect(connection).get_columns("fleet_stream_events")
            if column["name"] == "operator_note"
        )["nullable"]
        assert nullable is True


def test_postgres_reconciliation_releases_the_keys_of_a_retired_table(
    postgres_engine,
) -> None:
    """A retired table keeps its rows but its RESTRICT keys cannot block deletes.

    ``runtime_image_authorizations`` recorded who first used an image and is no
    longer read or written. An upgraded database still has it, with keys to the
    revision and build rows a collector deletes.
    """

    _upgrade(postgres_engine.url.render_as_string(hide_password=False))
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE runtime_image_authorizations ("
                "id VARCHAR(36) PRIMARY KEY, "
                "recipe_revision_id VARCHAR(36) NOT NULL, "
                "build_id VARCHAR(36) NOT NULL, "
                "CONSTRAINT fk_runtime_image_authorizations_recipe_revision "
                "FOREIGN KEY (recipe_revision_id) "
                "REFERENCES catalog_document_revisions (id) ON DELETE RESTRICT, "
                "CONSTRAINT fk_runtime_image_authorizations_build "
                "FOREIGN KEY (build_id) REFERENCES recipe_builds (id) "
                "ON DELETE RESTRICT)"
            )
        )
        assert inspect(connection).get_foreign_keys("runtime_image_authorizations")
    with postgres_engine.begin() as connection:
        verify_schema_is_current(connection)
    with postgres_engine.connect() as connection:
        inspector = inspect(connection)
        assert "runtime_image_authorizations" in inspector.get_table_names()
        assert inspector.get_foreign_keys("runtime_image_authorizations") == []


def test_schema_reconciliation_preserves_unexpected_column(
    tmp_path: Path,
) -> None:
    """``alembic_version`` is not evidence about the physical schema.

    A database created before a model change keeps the retired column while
    already reporting the current baseline, so the gate has to compare the live
    catalog and name the object, not trust the revision row.
    """

    url = f"sqlite:///{tmp_path / 'retired-column.sqlite'}"
    _upgrade(url)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            verify_schema_is_current(connection)
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE recipe_builds ADD COLUMN receipt_id VARCHAR(36)")
            )
        with engine.begin() as connection:
            verify_schema_is_current(connection)
        assert "receipt_id" in {
            column["name"] for column in inspect(engine).get_columns("recipe_builds")
        }
    finally:
        engine.dispose()


def test_postgres_schema_reconciliation_relaxes_retired_required_receipt_column(
    postgres_engine,
) -> None:
    """Reproduce the stale ``receipt_id NOT NULL`` column an upgraded NAS kept.

    The live deployment failed every runtime image authorization insert here,
    long after startup, and reported only SQLAlchemy's opaque ``gkpj`` code.
    """

    _upgrade(postgres_engine.url.render_as_string(hide_password=False))
    with postgres_engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE recipe_builds ADD COLUMN receipt_id VARCHAR(36) NOT NULL")
        )
    with postgres_engine.begin() as connection:
        verify_schema_is_current(connection)
    receipt_column = next(
        column
        for column in inspect(postgres_engine).get_columns("recipe_builds")
        if column["name"] == "receipt_id"
    )
    assert receipt_column["nullable"] is True


def test_postgres_startup_relaxes_changed_nullable_column_and_accepts_insert(
    postgres_engine,
) -> None:
    """Repeated startup applies wrapped nullable diffs before serial inserts."""
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    _upgrade(database_url)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO fleet_stream_events "
                "(event_type, node_id, entity_kind, entity_id, payload, occurred_at, expires_at) "
                "VALUES ('node-profile', 'node-a', 'node', 'node-a', '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '1 hour')"
            )
        )
        connection.execute(
            text("ALTER TABLE fleet_stream_events ALTER COLUMN node_id SET NOT NULL")
        )

    _initialize_source_checkout(database_url)
    _initialize_source_checkout(database_url)

    with postgres_engine.begin() as connection:
        inserted_id = connection.execute(
            text(
                "INSERT INTO fleet_stream_events "
                "(event_type, node_id, entity_kind, entity_id, payload, occurred_at, expires_at) "
                "VALUES ('node-profile', NULL, 'node', 'after-restart', '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '1 hour') "
                "RETURNING id"
            )
        ).scalar_one()
    assert inserted_id > 1


def test_postgres_startup_replaces_check_on_a_removed_column(postgres_engine) -> None:
    """An old check naming a column the model dropped must not block startup."""
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    _upgrade(database_url)
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE node_telemetry_samples "
                "ADD COLUMN cpu_utilization_percent DOUBLE PRECISION"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE node_telemetry_samples "
                "DROP CONSTRAINT ck_telemetry_utilization"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE node_telemetry_samples ADD CONSTRAINT ck_telemetry_utilization "
                "CHECK (cpu_utilization_percent IS NULL OR "
                "cpu_utilization_percent BETWEEN 0 AND 100)"
            )
        )

    _initialize_source_checkout(database_url)

    with postgres_engine.connect() as connection:
        verify_schema_is_current(connection)
        check = next(
            item
            for item in inspect(connection).get_check_constraints(
                "node_telemetry_samples"
            )
            if item["name"] == "ck_telemetry_utilization"
        )
    assert "cpu_utilization_percent" not in check["sqltext"]


def test_postgres_reconciliation_skips_lossy_type_narrowing_without_data_loss(
    postgres_engine,
) -> None:
    """A narrowing cast that truncates existing text is deferred safely."""
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    _upgrade(database_url)
    retained_value = "x" * 150
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE fleet_stream_events "
                "ALTER COLUMN entity_id TYPE VARCHAR(200)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO fleet_stream_events "
                "(event_type, entity_kind, entity_id, payload, occurred_at, expires_at) "
                "VALUES ('node-profile', 'node', :entity_id, '{}', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '1 hour')"
            ),
            {"entity_id": retained_value},
        )

    _initialize_source_checkout(database_url)

    with postgres_engine.connect() as connection:
        column = next(
            column
            for column in inspect(connection).get_columns("fleet_stream_events")
            if column["name"] == "entity_id"
        )
        assert column["type"].length == 200
        assert (
            connection.execute(
                text("SELECT entity_id FROM fleet_stream_events")
            ).scalar_one()
            == retained_value
        )


def test_postgres_schema_reconciliation_replaces_changed_check_expression(
    postgres_engine,
) -> None:
    """A same-named old check is replaced without blocking Controller startup."""
    _upgrade(postgres_engine.url.render_as_string(hide_password=False))
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE resource_reservations DROP CONSTRAINT ck_reservations_state"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE resource_reservations ADD CONSTRAINT ck_reservations_state CHECK (state IN ('active','released','expired') AND amount_bytes>=0)"
            )
        )
    with postgres_engine.begin() as connection:
        verify_schema_is_current(connection)
    with postgres_engine.connect() as connection:
        verify_schema_is_current(connection)


def test_postgres_startup_repairs_deployed_schema_and_is_idempotent(
    postgres_engine,
) -> None:
    """A prior deployed shape converges on startup and stays converged."""
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    _upgrade(database_url)
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE control_process_heartbeats "
            "DROP CONSTRAINT uq_control_process_heartbeats_instance"
        )
        connection.exec_driver_sql(
            "ALTER TABLE control_process_heartbeats "
            "ADD CONSTRAINT uq_control_process_heartbeats_kind UNIQUE (process_kind)"
        )
        connection.exec_driver_sql(
            "ALTER TABLE recipe_runs DROP CONSTRAINT ck_recipe_runs_recovery_attempts"
        )
        connection.exec_driver_sql(
            "ALTER TABLE recipe_runs DROP COLUMN recovery_attempts"
        )

    _initialize_source_checkout(database_url)
    assert set(Base.metadata.tables) <= _schema_tables(postgres_engine)
    columns = {
        column["name"]: column
        for column in inspect(postgres_engine).get_columns("recipe_runs")
    }
    assert columns["recovery_attempts"]["nullable"] is False
    assert columns["recovery_attempts"]["default"] in {"0", "0::integer"}
    heartbeat_uniques = {
        item["name"]: set(item["column_names"])
        for item in inspect(postgres_engine).get_unique_constraints(
            "control_process_heartbeats"
        )
    }
    assert heartbeat_uniques["uq_control_process_heartbeats_instance"] == {
        "process_kind",
        "process_instance_id",
    }
    assert "uq_control_process_heartbeats_kind" not in heartbeat_uniques
    checks = {
        item["name"]
        for item in inspect(postgres_engine).get_check_constraints("recipe_runs")
    }
    assert "ck_recipe_runs_recovery_attempts" in checks

    _initialize_source_checkout(database_url)
    assert set(Base.metadata.tables) <= _schema_tables(postgres_engine)


def test_postgres_reconciliation_keeps_equivalent_literal_array_casts(
    postgres_engine,
) -> None:
    """Server rendering changes must not falsely refuse a current database."""
    _upgrade(postgres_engine.url.render_as_string(hide_password=False))
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE resource_reservations DROP CONSTRAINT ck_reservations_state"
            )
        )
        connection.execute(
            text(
                "ALTER TABLE resource_reservations ADD CONSTRAINT ck_reservations_state CHECK ((state::text = ANY (ARRAY['active'::character varying::text, 'promised'::character varying::text, 'released'::character varying::text, 'expired'::character varying::text])) AND amount_bytes >= 0)"
            )
        )
    with postgres_engine.connect() as connection:
        verify_schema_is_current(connection)


def test_postgres_startup_adopts_heartbeat_step_columns(postgres_engine) -> None:
    """A heartbeat table from before the step columns gains them on startup."""
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    _upgrade(database_url)
    with postgres_engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE control_process_heartbeats DROP COLUMN running_step")
        )
        connection.execute(
            text("ALTER TABLE control_process_heartbeats DROP COLUMN step_started_at")
        )

    _initialize_source_checkout(database_url)

    with postgres_engine.connect() as connection:
        verify_schema_is_current(connection)
        columns = {
            item["name"]: item
            for item in inspect(connection).get_columns("control_process_heartbeats")
        }
    assert columns["running_step"]["nullable"] is True
    assert columns["step_started_at"]["nullable"] is True


_RESOURCE_SAMPLE_KEY = "fk_inventory_resource_sample"
_RESOURCE_SAMPLE_NODE = "spk_" + "c" * 32


def _store_sample(engine, sample_id: str) -> None:
    observed_at = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        if session.get(AgentNode, _RESOURCE_SAMPLE_NODE) is None:
            session.add(AgentNode(node_id=_RESOURCE_SAMPLE_NODE, state="active"))
            session.flush()
        session.add(
            telemetry_with_resources(
                NodeTelemetrySample(
                    id=sample_id,
                    node_id=_RESOURCE_SAMPLE_NODE,
                    boot_id="00000000-0000-4000-8000-000000000003",
                    observed_at=observed_at,
                    received_at=observed_at,
                ),
                HostResourceSample(
                    memory_total_bytes=None,
                    memory_available_bytes=None,
                    disk_total_bytes=None,
                    disk_free_bytes=None,
                ),
            )
        )


def _resource_sample_key(connection) -> tuple[str | None, bool] | None:
    """The live key's delete rule and whether PostgreSQL validated it."""

    row = connection.execute(
        text(
            "SELECT confdeltype, convalidated FROM pg_constraint "
            "WHERE conname = :name AND contype = 'f'"
        ),
        {"name": _RESOURCE_SAMPLE_KEY},
    ).first()
    return None if row is None else (row[0], row[1])


def _measurement_sources(engine) -> set[str]:
    with Session(engine) as session:
        return set(
            session.scalars(select(NodeInventoryResourceSnapshot.source_sample_id))
        )


def _startup_drift(database_url: str, caplog) -> dict[str, list[str]]:
    """Run startup and return the drift its end-of-reconcile warning reported."""

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="vonk_control.db"):
        _initialize_source_checkout(database_url)
    reported = [
        record.schema_drift
        for record in caplog.records
        if hasattr(record, "schema_drift")
    ]
    assert len(reported) <= 1
    return reported[0] if reported else {}


def test_postgres_startup_installs_a_missing_foreign_key_over_violating_rows(
    postgres_engine, caplog
) -> None:
    """Production lacked ``fk_inventory_resource_sample`` (2026-11-04).

    The key was declared after the table existed and reconciliation ignored
    Alembic's ``add_fk``, so 923,782 measurements outlived their sample. Startup
    installs the key although orphans violate it (SQLSTATE 23503), and the
    key's cascade then works for new deletes.
    """

    database_url = postgres_engine.url.render_as_string(hide_password=False)
    _upgrade(database_url)
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            f"ALTER TABLE node_inventory_snapshots DROP CONSTRAINT {_RESOURCE_SAMPLE_KEY}"
        )
    _store_sample(postgres_engine, "pruned-without-cascade")
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM node_telemetry_samples WHERE id = 'pruned-without-cascade'"
            )
        )

    assert _startup_drift(database_url, caplog) == {}

    with postgres_engine.connect() as connection:
        assert _resource_sample_key(connection) == ("c", False)
    # Reconciliation changes the schema only; the orphan is maintenance's.
    assert _measurement_sources(postgres_engine) == {"pruned-without-cascade"}
    _store_sample(postgres_engine, "pruned-with-cascade")
    with postgres_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM node_telemetry_samples WHERE id = 'pruned-with-cascade'")
        )
    assert _measurement_sources(postgres_engine) == {"pruned-without-cascade"}

    assert _startup_drift(database_url, caplog) == {}
    with postgres_engine.connect() as connection:
        assert _resource_sample_key(connection) == ("c", False)


def test_postgres_startup_replaces_a_foreign_key_whose_delete_rule_changed(
    postgres_engine, caplog
) -> None:
    """A live key on the declared columns with another rule is replaced in place."""

    database_url = postgres_engine.url.render_as_string(hide_password=False)
    _upgrade(database_url)
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            f"ALTER TABLE node_inventory_snapshots DROP CONSTRAINT {_RESOURCE_SAMPLE_KEY}"
        )
        connection.exec_driver_sql(
            f"ALTER TABLE node_inventory_snapshots ADD CONSTRAINT {_RESOURCE_SAMPLE_KEY} "
            "FOREIGN KEY (node_id, source_sample_id) "
            "REFERENCES node_telemetry_samples (node_id, id)"
        )

    assert _startup_drift(database_url, caplog) == {}

    with postgres_engine.connect() as connection:
        assert _resource_sample_key(connection) == ("c", True)


def test_postgres_startup_reports_what_reconciliation_leaves_in_place(
    postgres_engine, caplog
) -> None:
    """A live key the model never declared, and a difference no rule handles,
    stay in place and are named in the one end-of-reconcile warning."""

    database_url = postgres_engine.url.render_as_string(hide_password=False)
    _upgrade(database_url)
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE node_inventory_snapshots ADD CONSTRAINT fk_operator_extra "
            "FOREIGN KEY (node_id) REFERENCES agent_nodes (node_id)"
        )
        connection.exec_driver_sql("COMMENT ON TABLE agent_nodes IS 'operator note'")

    drift = _startup_drift(database_url, caplog)

    assert drift == {
        SchemaDriftKind.UNDECLARED_FOREIGN_KEY.value: [
            "node_inventory_snapshots.fk_operator_extra(node_id)"
        ],
        SchemaDriftKind.UNHANDLED_DIFFERENCE.value: [
            "remove_table_comment:agent_nodes"
        ],
    }
    with postgres_engine.connect() as connection:
        assert "fk_operator_extra" in {
            key["name"]
            for key in inspect(connection).get_foreign_keys("node_inventory_snapshots")
        }


def _declared_rule(rule: str | None) -> str:
    return (rule or "NO ACTION").upper()


def test_postgres_startup_reinstalls_every_declared_foreign_key(
    postgres_engine, caplog
) -> None:
    """Class guard: every key the model declares converges from its absence.

    All foreign keys of the model's tables are dropped, as on a database
    created before they were declared. One startup must reinstall each with its
    declared columns, target and delete rule, validated.
    """

    database_url = postgres_engine.url.render_as_string(hide_password=False)
    _upgrade(database_url)
    declared = sorted(
        (
            table.name,
            tuple(constraint.column_keys),
            constraint.referred_table.name,
            tuple(element.column.name for element in constraint.elements),
            _declared_rule(constraint.ondelete),
        )
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    )
    assert declared
    with postgres_engine.begin() as connection:
        for table_name, key_name in connection.execute(
            text(
                "SELECT conrelid::regclass::text, conname FROM pg_constraint "
                "WHERE contype = 'f' AND conrelid::regclass::text = ANY(:tables)"
            ),
            {"tables": sorted(Base.metadata.tables)},
        ).all():
            connection.exec_driver_sql(
                f'ALTER TABLE "{table_name}" DROP CONSTRAINT "{key_name}"'
            )

    assert _startup_drift(database_url, caplog) == {}

    with postgres_engine.connect() as connection:
        inspector = inspect(connection)
        live = sorted(
            (
                table_name,
                tuple(key["constrained_columns"]),
                key["referred_table"],
                tuple(key["referred_columns"]),
                _declared_rule(key["options"].get("ondelete")),
            )
            for table_name in Base.metadata.tables
            for key in inspector.get_foreign_keys(table_name)
        )
        assert live == declared
        assert not connection.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE contype = 'f' AND NOT convalidated"
            )
        ).all()


def test_unaddable_missing_column_fails_reconciliation_instead_of_skipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches booting without a column whose add timed out on a lock."""
    from alembic.operations import Operations
    from sqlalchemy.exc import OperationalError

    class LockTimeout(Exception):
        sqlstate = "55P03"

    url = f"sqlite:///{tmp_path / 'unaddable-column.sqlite'}"
    _upgrade(url)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE control_process_heartbeats DROP COLUMN running_step")
            )

        def timed_out(*_args: object, **_kwargs: object) -> None:
            raise OperationalError("ALTER TABLE", {}, LockTimeout("lock timeout"))

        monkeypatch.setattr(Operations, "add_column", timed_out)
        with engine.connect() as connection, pytest.raises(OperationalError):
            verify_schema_is_current(connection)
    finally:
        engine.dispose()


def test_missing_column_left_after_reconciliation_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a column add that silently did nothing and let startup finish."""
    from alembic.operations import Operations

    url = f"sqlite:///{tmp_path / 'net-column.sqlite'}"
    _upgrade(url)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE control_process_heartbeats DROP COLUMN running_step")
            )
        monkeypatch.setattr(Operations, "add_column", lambda *_a, **_k: None)
        with (
            engine.connect() as connection,
            pytest.raises(
                RuntimeError, match="control_process_heartbeats.running_step"
            ),
        ):
            verify_schema_is_current(connection)
    finally:
        engine.dispose()
