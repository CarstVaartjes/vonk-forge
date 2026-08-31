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
    "agent_upgrade_compatibility_recoveries",
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
    compatibility_columns = {
        column["name"]: column
        for column in inspect(engine).get_columns(
            "agent_upgrade_compatibility_recoveries"
        )
    }
    assert compatibility_columns["state"]["type"].length == 32
    assert compatibility_columns["identity_deadline"]["nullable"] is True
    assert compatibility_columns["rearm_attempt_certificate_serial"]["nullable"] is True
    assert (
        compatibility_columns["rearm_dispatch_certificate_serial"]["nullable"] is True
    )
    assert compatibility_columns["abandoned_at"]["nullable"] is True
    compatibility_checks = {
        constraint["name"]
        for constraint in inspect(engine).get_check_constraints(
            "agent_upgrade_compatibility_recoveries"
        )
    }
    assert {
        "ck_agent_upgrade_compatibility_recoveries_grant_all_or_none",
        "ck_agent_upgrade_compatibility_recoveries_rearm_certs_paired",
        "ck_agent_upgrade_compatibility_recoveries_retry_matches_rearm",
        "ck_agent_upgrade_compatibility_recoveries_state_fields",
    } <= compatibility_checks
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
        "0006_spark3542_compat_recovery.py",
        "0007_compat_recovery_rearm_certificates.py",
        "0008_compat_recovery_abandon.py",
        "0009_compat_abandoned_at.py",
    ]


def test_compatibility_rearm_certificate_migration_preserves_rows_and_constraints(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'compatibility-rearm-upgrade.sqlite'}"
    config = _config(url)
    command.upgrade(config, "0006_spark3542_compat_recovery")
    engine = create_engine(url)
    insert_recovery = text(
        """
        INSERT INTO agent_upgrade_compatibility_recoveries
          (id, node_id, job_id, operation_id, source_attempt, source_fence,
           source_certificate_serial, expected_retry_attempt,
           source_semantic_version, source_build_digest, source_binary_digest,
           upgrade_payload_sha256, package_sha256, target_package_version,
           target_build_digest, target_binary_digest, authority_revision,
           plan_digest, state, actor, request_id, created_at)
        VALUES
          ('recovery', 'node', 'job', 'operation', 3, 'source-fence',
           'source-certificate', 4, '0.1.0', :source_build_digest,
           :source_binary_digest, :upgrade_payload_sha256, :package_sha256,
           '0.1.0~dev.381', :target_build_digest, :target_binary_digest,
           'authority', :plan_digest, 'armed', 'admin', 'request',
           '2026-08-31 12:00:00')
        """
    )
    digests = {
        "source_build_digest": f"sha256:{'a' * 64}",
        "source_binary_digest": "b" * 64,
        "upgrade_payload_sha256": "c" * 64,
        "package_sha256": "d" * 64,
        "target_build_digest": f"sha256:{'e' * 64}",
        "target_binary_digest": "f" * 64,
        "plan_digest": "0" * 64,
    }
    with engine.begin() as connection:
        connection.execute(insert_recovery, digests)

    command.upgrade(config, "head")

    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT rearm_attempt_certificate_serial, "
                "rearm_dispatch_certificate_serial "
                "FROM agent_upgrade_compatibility_recoveries WHERE id = 'recovery'"
            )
        ).one() == (None, None)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE agent_upgrade_compatibility_recoveries "
                "SET rearm_attempt_certificate_serial = 'attempt-certificate' "
                "WHERE id = 'recovery'"
            )
        )

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE agent_upgrade_compatibility_recoveries "
                "SET rearm_attempt_certificate_serial = 'attempt-certificate', "
                "rearm_dispatch_certificate_serial = 'dispatch-certificate' "
                "WHERE id = 'recovery'"
            )
        )

    issue_grant = text(
        """
        UPDATE agent_upgrade_compatibility_recoveries
        SET state = 'issued', retry_fence = 'retry-fence',
            retry_certificate_serial = :retry_certificate_serial,
            signed_grant = '{}', grant_request_id = 'grant-request',
            grant_expires_at = '2026-08-31 12:00:10',
            identity_deadline = '2026-08-31 12:15:00',
            issued_at = '2026-08-31 12:00:00'
        WHERE id = 'recovery'
        """
    )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            issue_grant, {"retry_certificate_serial": "other-certificate"}
        )

    with engine.begin() as connection:
        connection.execute(
            issue_grant, {"retry_certificate_serial": "dispatch-certificate"}
        )
        connection.execute(
            text(
                "UPDATE agent_upgrade_compatibility_recoveries "
                "SET state = 'operator-blocked', blocked_at = '2026-08-31 12:15:00' "
                "WHERE id = 'recovery'"
            )
        )
        connection.execute(
            text(
                "UPDATE agent_upgrade_compatibility_recoveries "
                "SET state = 'abandoned', completed_at = '2026-08-31 12:16:00', "
                "abandoned_at = '2026-08-31 12:16:00' "
                "WHERE id = 'recovery'"
            )
        )

    command.downgrade(config, "0007_compat_rearm_certificates")
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT state, completed_at FROM "
                "agent_upgrade_compatibility_recoveries WHERE id = 'recovery'"
            )
        ).one() == ("operator-blocked", None)


def test_compatibility_abandoned_at_migration_backfills_terminal_rows(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'compatibility-abandoned-at-upgrade.sqlite'}"
    config = _config(url)
    command.upgrade(config, "0008_compat_recovery_abandon")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO agent_upgrade_compatibility_recoveries
                  (id, node_id, job_id, operation_id, source_attempt,
                   source_fence, source_certificate_serial,
                   expected_retry_attempt, source_semantic_version,
                   source_build_digest, source_binary_digest,
                   upgrade_payload_sha256, package_sha256,
                   target_package_version, target_build_digest,
                   target_binary_digest, authority_revision, plan_digest,
                   state, actor, request_id, created_at, completed_at,
                   blocked_at)
                VALUES
                  ('recovery', 'node', 'job', 'operation', 3, 'source-fence',
                   'source-certificate', 4, '0.1.0', :source_build_digest,
                   :source_binary_digest, :upgrade_payload_sha256,
                   :package_sha256, '0.1.0~dev.381', :target_build_digest,
                   :target_binary_digest, 'authority', :plan_digest,
                   'abandoned', 'admin', 'request',
                   '2026-08-31 12:00:00', '2026-08-31 12:16:00',
                   '2026-08-31 12:15:00')
                """
            ),
            {
                "source_build_digest": f"sha256:{'a' * 64}",
                "source_binary_digest": "b" * 64,
                "upgrade_payload_sha256": "c" * 64,
                "package_sha256": "d" * 64,
                "target_build_digest": f"sha256:{'e' * 64}",
                "target_binary_digest": "f" * 64,
                "plan_digest": "0" * 64,
            },
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state, completed_at, abandoned_at FROM "
                "agent_upgrade_compatibility_recoveries WHERE id = 'recovery'"
            )
        ).one()
        assert row == (
            "abandoned",
            "2026-08-31 12:16:00",
            "2026-08-31 12:16:00",
        )


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
            == "0009_compat_abandoned_at"
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
            == "0009_compat_abandoned_at"
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
