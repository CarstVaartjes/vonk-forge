from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

EXPECTED_BASELINE_TABLES = {
    "artifact_job_blobs",
    "artifact_job_files",
    "artifact_jobs",
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
    "fleet_profile_applications",
    "fleet_profiles",
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


def test_fresh_install_has_an_ordered_forward_migration_chain() -> None:
    versions = Path(__file__).resolve().parents[1] / "migrations/versions"

    assert sorted(path.name for path in versions.glob("*.py")) == [
        "0001_fleet_library_baseline.py",
        "0002_fleet_node_profile_events.py",
        "0003_agent_reenrollment_grants.py",
        "0004_artifact_jobs.py",
        "0005_repair_fleet_profile_tables.py",
    ]


def test_existing_baseline_is_upgraded_to_accept_node_profile_events(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'upgrade.sqlite'}"
    config = _config(url)
    command.upgrade(config, "0001_fleet_library_baseline")
    engine = create_engine(url)
    statement = text(
        """
        INSERT INTO fleet_stream_events
          (id, event_type, node_id, entity_kind, entity_id, payload,
           occurred_at, expires_at)
        VALUES
          (1, 'node-profile', :node_id, 'node-profile', :node_id, '{}',
           '2026-08-25 12:00:00', '2026-08-26 12:00:00')
        """
    )
    node_id = "spk_" + "a" * 32

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(statement, {"node_id": node_id})

    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(statement, {"node_id": node_id})
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0005_repair_fleet_profile_tables"
        )


def test_existing_database_missing_fleet_profile_tables_is_repaired(
    tmp_path: Path,
) -> None:
    from vonk_control.models import Base

    url = f"sqlite:///{tmp_path / 'fleet-profile-repair.sqlite'}"
    config = _config(url)
    command.upgrade(config, "0004_artifact_jobs")
    engine = create_engine(url)

    # Reproduce databases that applied the original 0001 before fleet profiles
    # were added to that already-released baseline migration.
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE fleet_profile_applications"))
        connection.execute(text("DROP TABLE fleet_profiles"))
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0004_artifact_jobs"
        )

    command.upgrade(config, "head")

    inspector = inspect(engine)
    assert {"fleet_profiles", "fleet_profile_applications"} <= set(
        inspector.get_table_names()
    )
    assert {index["name"] for index in inspector.get_indexes("fleet_profiles")} == {
        "ix_fleet_profiles_created_at",
        "ix_fleet_profiles_updated_at",
    }
    assert {
        index["name"] for index in inspector.get_indexes("fleet_profile_applications")
    } == {
        "ix_fleet_profile_applications_created_at",
        "ix_fleet_profile_applications_current_operation_id",
        "ix_fleet_profile_applications_profile_id",
        "ix_fleet_profile_applications_state",
    }
    with engine.connect() as connection:
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO fleet_profiles
                  (id, name, description, installation_policy, assignments,
                   labels, favorite, created_by, created_at, updated_at)
                VALUES
                  ('profile', 'Qualification', '', 'keep-cached', '[]', '{}',
                   0, 'admin', '2026-08-28 12:00:00', '2026-08-28 12:00:00')
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO fleet_profile_applications
                  (id, request_key, profile_id, profile_digest, plan_digest,
                   state, plan, current_step, current_operation_id, progress,
                   result, status_reason, actor, created_at, updated_at)
                VALUES
                  ('application', 'request', 'profile', :profile_digest,
                   :plan_digest, 'queued', '{}', 0, NULL, '{}', NULL, NULL,
                   'admin', '2026-08-28 12:00:00', '2026-08-28 12:00:00')
                """
            ),
            {"profile_digest": "a" * 64, "plan_digest": "b" * 64},
        )
        assert (
            connection.execute(
                text("SELECT profile_id FROM fleet_profile_applications")
            ).scalar_one()
            == "profile"
        )
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0005_repair_fleet_profile_tables"
        )


def test_existing_database_is_upgraded_to_accept_reenrollment_grants(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'reenrollment-upgrade.sqlite'}"
    config = _config(url)
    command.upgrade(config, "0002_fleet_node_profile_events")
    engine = create_engine(url)
    statement = text(
        """
        INSERT INTO agent_enrollment_grants
          (id, node_id, purpose, token_digest, created_by, created_at, expires_at)
        VALUES
          ('grant', NULL, 're-enroll', :digest, 'admin',
           '2026-08-25 12:00:00', '2026-08-25 12:10:00')
        """
    )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(statement, {"digest": "a" * 64})

    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(statement, {"digest": "a" * 64})
        assert (
            connection.execute(
                text("SELECT purpose FROM agent_enrollment_grants WHERE id = 'grant'")
            ).scalar_one()
            == "re-enroll"
        )


def test_fresh_baseline_is_reversible(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'control.sqlite'}"
    config = _config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    tables = set(inspect(create_engine(url)).get_table_names())
    assert tables <= {"alembic_version"}
