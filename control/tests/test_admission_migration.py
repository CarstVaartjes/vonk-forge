from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from vonk_control import models
from vonk_control.models import FleetEventCursor, FleetStreamEvent


def config(url: str) -> Config:
    root = Path(__file__).resolve().parents[1]; value = Config(root / "alembic.ini")
    value.set_main_option("script_location", str(root / "migrations")); value.set_main_option("sqlalchemy.url", url); return value


def test_database_migrations_are_one_exact_linear_chain_at_0025() -> None:
    script = ScriptDirectory.from_config(config("sqlite://"))
    assert script.get_heads() == ["0025_telemetry_retention"]
    assert (
        script.get_revision("0025_telemetry_retention").down_revision
        == "0024_fleet_stream_events"
    )
    assert [
        revision.revision
        for revision in reversed(tuple(script.walk_revisions()))
    ] == [
        "0001_operational_state",
        "0002_agent_operations",
        "0003_retry_disposition",
        "0004_agent_enrollment",
        "0005_certificate_rotation",
        "0006_reconciliation_graph",
        "0007_issued_revocations",
        "0008_resolved_plan",
        "0009_reconciliation_execution",
        "0010_agent_runtime_identity",
        "0011_update_rollouts",
        "0012_control_process_heartbeats",
        "0013_workload_packages",
        "0014_package_action_plans",
        "0015_recipe_catalog",
        "0016_recipe_deployment_authority",
        "0017_admission_and_run_state",
        "0018_agent_inventory_runtime",
        "0019_rust_agent_migration",
        "0020_recipe_catalog_bridge",
        "0021_browser_authentication",
        "0022_observation_latest_index",
        "0023_node_telemetry",
        "0024_fleet_stream_events",
        "0025_telemetry_retention",
    ]


def test_admission_tables_upgrade_and_downgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path/'admission.sqlite'}"; command.upgrade(config(url), "head"); engine = create_engine(url)
    tables = {"node_inventory_snapshots", "node_artifacts", "resource_reservations", "recipe_installations", "installation_nodes", "recipe_runs", "run_nodes", "node_telemetry_samples", "node_telemetry_latest"}
    assert tables <= set(inspect(engine).get_table_names())
    assert {"recipe_revision_id", "plan_digest", "state"} <= {column["name"] for column in inspect(engine).get_columns("recipe_installations")}
    assert {"reserved_memory_bytes", "observed_memory_bytes", "rank", "role"} <= {column["name"] for column in inspect(engine).get_columns("run_nodes")}
    assert {
        "node_id",
        "boot_id",
        "sequence",
        "observed_at",
        "received_at",
        "cpu_utilization_percent",
        "memory_available_bytes",
        "gpu_utilization_percent",
        "details",
    } <= {
        column["name"]
        for column in inspect(engine).get_columns("node_telemetry_samples")
    }
    assert {"node_id", "sample_id"} == {
        column["name"]
        for column in inspect(engine).get_columns("node_telemetry_latest")
    }
    sample_uniques = {
        (constraint["name"], tuple(constraint["column_names"]))
        for constraint in inspect(engine).get_unique_constraints(
            "node_telemetry_samples"
        )
    }
    assert (
        "uq_telemetry_node_sample",
        ("node_id", "id"),
    ) in sample_uniques
    latest_foreign_keys = inspect(engine).get_foreign_keys("node_telemetry_latest")
    assert any(
        foreign_key["constrained_columns"] == ["node_id", "sample_id"]
        and foreign_key["referred_table"] == "node_telemetry_samples"
        and foreign_key["referred_columns"] == ["node_id", "id"]
        for foreign_key in latest_foreign_keys
    )
    checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspect(engine).get_check_constraints(
            "node_telemetry_samples"
        )
    }
    capacity_limit = "17592186044416"
    assert all(
        capacity_limit in checks[name]
        for name in (
            "ck_telemetry_memory",
            "ck_telemetry_disk",
            "ck_telemetry_gpu_memory",
        )
    )
    assert "1000000000000000" in checks["ck_telemetry_physical_metrics"]
    command.downgrade(config(url), "0016_recipe_deployment_authority")
    assert not tables & set(inspect(engine).get_table_names())


def test_fleet_event_tables_upgrade_seed_and_downgrade_at_0024(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'fleet-events.sqlite'}"
    migration_config = config(url)
    command.upgrade(migration_config, "0024_fleet_stream_events")
    engine = create_engine(url)
    inspector = inspect(engine)

    assert {"fleet_event_cursor", "fleet_stream_events"} <= set(
        inspector.get_table_names()
    )
    assert [column["name"] for column in inspector.get_columns("fleet_event_cursor")] == [
        "singleton_id",
        "last_id",
    ]
    assert [column["name"] for column in inspector.get_columns("fleet_stream_events")] == [
        "id",
        "event_type",
        "node_id",
        "entity_kind",
        "entity_id",
        "payload",
        "occurred_at",
        "expires_at",
    ]
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints("fleet_stream_events")
    } == {
        "ck_fleet_stream_events_event_type",
        "ck_fleet_stream_events_expiry",
        "ck_fleet_stream_events_payload_size",
    }
    assert {
        (index["name"], tuple(index["column_names"]))
        for index in inspector.get_indexes("fleet_stream_events")
    } == {
        ("ix_fleet_stream_events_expires_id", ("expires_at", "id")),
        ("ix_fleet_stream_events_node_id", ("node_id", "id")),
    }
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT singleton_id,last_id FROM fleet_event_cursor")
        ).one() == (1, 0)

    command.downgrade(migration_config, "0023_node_telemetry")
    assert not {"fleet_event_cursor", "fleet_stream_events"} & set(
        inspect(engine).get_table_names()
    )


def test_0024_rejects_payload_over_8192_utf8_bytes(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'fleet-events-byte-bound.sqlite'}"
    command.upgrade(config(url), "0024_fleet_stream_events")
    engine = create_engine(url)
    payload = '{"value":"' + "\N{GRINNING FACE}" * 3000 + '"}'
    assert len(payload) < 8192
    assert len(payload.encode("utf-8")) > 8192

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO fleet_stream_events (
                    id, event_type, node_id, entity_kind, entity_id,
                    payload, occurred_at, expires_at
                ) VALUES (
                    1, 'operation-state', NULL, 'job', 'job-multibyte',
                    :payload, :occurred_at, :expires_at
                )
                """
            ),
            {
                "payload": payload,
                "occurred_at": "2026-08-15T12:00:00+00:00",
                "expires_at": "2026-08-16T12:00:00+00:00",
            },
        )


def test_fleet_event_migration_and_model_metadata_have_exact_schema_parity(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'fleet-events-parity.sqlite'}"
    command.upgrade(config(url), "0024_fleet_stream_events")
    engine = create_engine(url)
    inspector = inspect(engine)

    for model_table in (FleetEventCursor.__table__, FleetStreamEvent.__table__):
        reflected_columns = [
            (
                column["name"],
                str(column["type"].compile(dialect=engine.dialect)),
                column["nullable"],
                column["primary_key"],
            )
            for column in inspector.get_columns(model_table.name)
        ]
        model_columns = [
            (
                column.name,
                str(column.type.compile(dialect=engine.dialect)),
                column.nullable,
                column.primary_key,
            )
            for column in model_table.columns
        ]
        assert reflected_columns == model_columns
        assert {
            constraint["name"]: " ".join(constraint["sqltext"].split())
            for constraint in inspector.get_check_constraints(model_table.name)
        } == {
            constraint.name: " ".join(
                str(
                    constraint.sqltext.compile(
                        dialect=engine.dialect,
                        compile_kwargs={"literal_binds": True},
                    )
                ).split()
            )
            for constraint in model_table.constraints
            if constraint.name is not None
        }
        assert {
            (index["name"], tuple(index["column_names"]))
            for index in inspector.get_indexes(model_table.name)
        } == {
            (index.name, tuple(index.columns.keys())) for index in model_table.indexes
        }


def test_telemetry_rollup_tables_upgrade_and_downgrade_at_0025(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'telemetry-retention.sqlite'}"
    migration_config = config(url)
    command.upgrade(migration_config, "0025_telemetry_retention")
    engine = create_engine(url)
    inspector = inspect(engine)
    table_names = {
        "node_telemetry_rollup_buckets",
        "node_telemetry_rollup_metrics",
        "node_telemetry_rollup_dirty",
    }

    assert table_names <= set(inspector.get_table_names())
    assert [
        column["name"]
        for column in inspector.get_columns("node_telemetry_rollup_buckets")
    ] == [
        "resolution_seconds",
        "node_id",
        "bucket_start",
        "source_sample_count",
        "gap_samples",
    ]
    assert [
        column["name"]
        for column in inspector.get_columns("node_telemetry_rollup_metrics")
    ] == [
        "resolution_seconds",
        "node_id",
        "bucket_start",
        "metric_name",
        "sample_count",
        "minimum",
        "mean",
        "maximum",
    ]
    assert [
        column["name"]
        for column in inspector.get_columns("node_telemetry_rollup_dirty")
    ] == ["resolution_seconds", "node_id", "bucket_start"]

    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "node_telemetry_rollup_buckets"
        )
    } == {
        "ck_telemetry_rollup_buckets_resolution",
        "ck_telemetry_rollup_buckets_counts",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "node_telemetry_rollup_metrics"
        )
    } == {
        "ck_telemetry_rollup_metrics_resolution",
        "ck_telemetry_rollup_metrics_name",
        "ck_telemetry_rollup_metrics_count",
        "ck_telemetry_rollup_metrics_values",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_check_constraints(
            "node_telemetry_rollup_dirty"
        )
    } == {"ck_telemetry_rollup_dirty_resolution"}
    assert {
        (index["name"], tuple(index["column_names"]))
        for index in inspector.get_indexes("node_telemetry_rollup_buckets")
    } == {
        (
            "ix_telemetry_rollup_buckets_resolution_start",
            ("resolution_seconds", "bucket_start", "node_id"),
        )
    }
    assert {
        (index["name"], tuple(index["column_names"]))
        for index in inspector.get_indexes("node_telemetry_rollup_dirty")
    } == {
        (
            "ix_telemetry_rollup_dirty_resolution_start",
            ("resolution_seconds", "bucket_start", "node_id"),
        )
    }

    bucket_foreign_keys = inspector.get_foreign_keys(
        "node_telemetry_rollup_buckets"
    )
    assert any(
        foreign_key["constrained_columns"] == ["node_id"]
        and foreign_key["referred_table"] == "agent_nodes"
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in bucket_foreign_keys
    )
    metric_foreign_keys = inspector.get_foreign_keys(
        "node_telemetry_rollup_metrics"
    )
    assert any(
        foreign_key["constrained_columns"]
        == ["resolution_seconds", "node_id", "bucket_start"]
        and foreign_key["referred_table"]
        == "node_telemetry_rollup_buckets"
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in metric_foreign_keys
    )
    dirty_foreign_keys = inspector.get_foreign_keys(
        "node_telemetry_rollup_dirty"
    )
    assert any(
        foreign_key["constrained_columns"] == ["node_id"]
        and foreign_key["referred_table"] == "agent_nodes"
        and foreign_key["options"].get("ondelete") == "CASCADE"
        for foreign_key in dirty_foreign_keys
    )

    command.downgrade(migration_config, "0024_fleet_stream_events")
    assert not table_names & set(inspect(engine).get_table_names())


def test_telemetry_rollup_migration_and_models_have_exact_schema_parity(
    tmp_path: Path,
) -> None:
    assert hasattr(models, "NodeTelemetryRollupBucket")
    assert hasattr(models, "NodeTelemetryRollupMetric")
    assert hasattr(models, "NodeTelemetryRollupDirty")

    url = f"sqlite:///{tmp_path / 'telemetry-retention-parity.sqlite'}"
    command.upgrade(config(url), "0025_telemetry_retention")
    engine = create_engine(url)
    inspector = inspect(engine)
    model_tables = (
        models.NodeTelemetryRollupBucket.__table__,
        models.NodeTelemetryRollupMetric.__table__,
        models.NodeTelemetryRollupDirty.__table__,
    )

    for model_table in model_tables:
        reflected_columns = [
            (
                column["name"],
                str(column["type"].compile(dialect=engine.dialect)),
                column["nullable"],
                bool(column["primary_key"]),
            )
            for column in inspector.get_columns(model_table.name)
        ]
        model_columns = [
            (
                column.name,
                str(column.type.compile(dialect=engine.dialect)),
                column.nullable,
                column.primary_key,
            )
            for column in model_table.columns
        ]
        assert reflected_columns == model_columns
        assert {
            constraint["name"]: " ".join(constraint["sqltext"].split())
            for constraint in inspector.get_check_constraints(model_table.name)
        } == {
            constraint.name: " ".join(
                str(
                    constraint.sqltext.compile(
                        dialect=engine.dialect,
                        compile_kwargs={"literal_binds": True},
                    )
                ).split()
            )
            for constraint in model_table.constraints
            if isinstance(constraint, CheckConstraint)
            and constraint.name is not None
        }
        assert {
            (index["name"], tuple(index["column_names"]))
            for index in inspector.get_indexes(model_table.name)
        } == {
            (index.name, tuple(index.columns.keys())) for index in model_table.indexes
        }
