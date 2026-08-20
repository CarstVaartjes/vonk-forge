from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

EXPECTED_BASELINE_TABLES = {
    "control_authority_heads",
    "control_authority_proposals",
    "control_authority_revisions",
    "agent_certificate_rotations",
    "agent_certificates",
    "agent_enrollment_grants",
    "agent_enrollments",
    "agent_issued_certificate_revocations",
    "agent_node_profiles",
    "agent_profiles",
    "agent_nodes",
    "agent_operation_attempts",
    "agent_operations",
    "agent_presence",
    "audit_events",
    "catalog_entities",
    "catalog_entity_revisions",
    "cluster_mapping_nodes",
    "cluster_mappings",
    "control_process_heartbeats",
    "fleet_event_cursor",
    "fleet_stream_events",
    "installation_nodes",
    "job_attempts",
    "job_log_entries",
    "jobs",
    "local_recipe_revisions",
    "local_recipes",
    "node_artifacts",
    "node_inventory_snapshots",
    "node_mutation_leases",
    "node_telemetry_latest",
    "node_telemetry_rollup_buckets",
    "node_telemetry_rollup_dirty",
    "node_telemetry_rollup_metrics",
    "node_telemetry_samples",
    "observations",
    "recipe_builds",
    "recipe_global_links",
    "recipe_import_items",
    "recipe_imports",
    "recipe_installations",
    "recipe_runs",
    "recipe_source_bundles",
    "source_bundle_archives",
    "recipe_revisions",
    "recipes",
    "recipe_test_reports",
    "reconciliation_cancellations",
    "reconciliation_completion_generation",
    "reconciliation_operations",
    "reconciliations",
    "resource_reservations",
    "route_publication_owner",
    "route_publications",
    "run_nodes",
    "sessions",
    "telemetry_maintenance_state",
    "update_authorization_intents",
    "update_rollout_nodes",
    "update_rollouts",
    "users",
}


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_fresh_baseline_creates_retained_metadata_without_legacy_tables(
    tmp_path: Path,
) -> None:
    from vonk_control.models import Base

    url = f"sqlite:///{tmp_path / 'control.sqlite'}"
    config = _config(url)
    command.upgrade(config, "head")

    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())

    assert tables - {"alembic_version"} == EXPECTED_BASELINE_TABLES
    assert set(Base.metadata.tables) == EXPECTED_BASELINE_TABLES
    assert "agent_node_profiles" in tables
    assert not any(table.startswith("package_") for table in tables)
    with engine.connect() as connection:
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )
        assert connection.execute(
            text(
                "SELECT singleton_id, next_resolution_seconds FROM telemetry_maintenance_state"
            )
        ).all() == [(1, 60)]
        assert connection.execute(
            text("SELECT singleton_id, last_id FROM fleet_event_cursor")
        ).all() == [(1, 0)]


def test_fresh_baseline_is_fixed_and_does_not_import_live_metadata() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/0001_fleet_library_baseline.py"
    ).read_text()

    assert "vonk_control.models" not in migration
    assert "Base.metadata" not in migration
    assert ".create_all(" not in migration


def test_fresh_install_has_one_baseline_migration() -> None:
    versions = Path(__file__).resolve().parents[1] / "migrations/versions"

    assert sorted(path.name for path in versions.glob("*.py")) == [
        "0001_fleet_library_baseline.py"
    ]


def test_fresh_baseline_is_reversible(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'control.sqlite'}"
    config = _config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    tables = set(inspect(create_engine(url)).get_table_names())
    assert tables <= {"alembic_version"}
