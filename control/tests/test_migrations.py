import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

EXPECTED_BASELINE_TABLES = {
    "artifact_distribution_assignments",
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
    "catalog_documents",
    "catalog_document_heads",
    "catalog_document_revisions",
    "catalog_recipe_model_references",
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
    "managed_recipe_library_links",
    "model_cache_artifacts",
    "model_cache_operations",
    "model_cache_set_artifacts",
    "model_cache_sets",
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
    "recipe_library_sync_runs",
    "recipe_run_observation_grants",
    "recipe_runs",
    "recipe_source_bundles",
    "source_bundle_archives",
    "recipe_test_reports",
    "runtime_image_receipts",
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
RETAINED_LEGACY_TABLES = {"agent_upgrade_compatibility_recoveries"}


def _metadata_differences_without_retained_legacy(connection: object) -> list[object]:
    from vonk_control.models import Base

    differences = compare_metadata(
        MigrationContext.configure(connection), Base.metadata
    )
    return [
        difference
        for difference in differences
        if not (
            (
                difference[0] == "remove_table"
                and difference[1].name in RETAINED_LEGACY_TABLES
            )
            or (
                difference[0] == "remove_index"
                and difference[1].table.name in RETAINED_LEGACY_TABLES
            )
        )
    ]


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_fresh_baseline_creates_retained_metadata_with_inert_legacy_storage(
    tmp_path: Path,
) -> None:
    from vonk_control.models import Base

    url = f"sqlite:///{tmp_path / 'control.sqlite'}"
    config = _config(url)
    command.upgrade(config, "head")

    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())

    assert tables - {"alembic_version"} - RETAINED_LEGACY_TABLES == (
        EXPECTED_BASELINE_TABLES
    )
    assert set(Base.metadata.tables) == EXPECTED_BASELINE_TABLES
    assert "agent_node_profiles" in tables
    assert not any(table.startswith("package_") for table in tables)
    with engine.connect() as connection:
        assert _metadata_differences_without_retained_legacy(connection) == []
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
        "0000_canonical_catalog_baseline.py",
        "0001_fleet_library_baseline.py",
        "0002_fleet_node_profile_events.py",
        "0003_agent_reenrollment_grants.py",
        "0004_artifact_jobs.py",
        "0005_repair_fleet_profile_tables.py",
        "0006_spark3542_compat_recovery.py",
        "0007_compat_recovery_rearm_certificates.py",
        "0008_compat_recovery_abandon.py",
        "0009_compat_abandoned_at.py",
        "0010_managed_recipe_catalog_sync.py",
        "0011_recipe_model_identity.py",
        "0012_recipe_run_generation.py",
        "0013_repeatable_install_plans.py",
        "0014_fleet_profile_scope.py",
        "0015_model_cache.py",
        "0016_rich_telemetry_metrics.py",
        "0017_artifact_distribution_assignments.py",
        "0018_canonical_catalog_documents.py",
        "0019_recipe_builds_canonical_revision.py",
        "0020_runtime_image_receipts.py",
    ]


def test_fresh_baseline_binds_recipe_builds_to_canonical_recipe_revision(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'recipe-build-fk.sqlite'}"
    config = _config(url)
    command.upgrade(config, "head")
    engine = create_engine(url)

    foreign_keys = inspect(engine).get_foreign_keys("recipe_builds")
    recipe_revision_keys = [
        foreign_key
        for foreign_key in foreign_keys
        if foreign_key["constrained_columns"] == ["recipe_revision_id"]
    ]

    assert len(recipe_revision_keys) == 1
    assert recipe_revision_keys[0]["referred_table"] == "catalog_document_revisions"
    assert recipe_revision_keys[0]["referred_columns"] == ["id"]
    assert all(
        foreign_key["referred_table"] != "local_recipe_revisions"
        for foreign_key in recipe_revision_keys
    )


def test_existing_compatibility_recovery_revision_upgrades_without_operational_model(
    tmp_path: Path,
) -> None:
    from vonk_control.models import Base

    url = f"sqlite:///{tmp_path / 'compat-recovery-upgrade.sqlite'}"
    config = _config(url)
    command.upgrade(config, "0009_compat_abandoned_at")
    engine = create_engine(url)

    assert "agent_upgrade_compatibility_recoveries" in set(
        inspect(engine).get_table_names()
    )
    assert "agent_upgrade_compatibility_recoveries" not in Base.metadata.tables

    timestamp = "2026-08-29 12:00:00+00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs "
                "(id, request_id, kind, state, actor, authority_revision, targets, "
                "payload_digest, payload, current_attempt, created_at, updated_at) "
                "VALUES "
                "(:id, :request_id, :kind, :state, :actor, :authority_revision, "
                ":targets, :payload_digest, :payload, :current_attempt, :created_at, "
                ":updated_at)"
            ),
            {
                "id": "legacy-job",
                "request_id": "legacy-job-request",
                "kind": "agent.upgrade.v1",
                "state": "failed",
                "actor": "migration-regression",
                "authority_revision": "legacy-authority",
                "targets": '["legacy-node"]',
                "payload_digest": "1" * 64,
                "payload": "{}",
                "current_attempt": 1,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO agent_nodes "
                "(node_id, state, self_test_passed, capabilities) "
                "VALUES (:node_id, :state, :self_test_passed, :capabilities)"
            ),
            {
                "node_id": "legacy-node",
                "state": "active",
                "self_test_passed": True,
                "capabilities": '["agent.upgrade.v1"]',
            },
        )
        connection.execute(
            text(
                "INSERT INTO agent_operations "
                "(id, parent_job_id, node_id, kind, payload_digest, payload, "
                "authority_revision, state, current_attempt, created_at, updated_at) "
                "VALUES "
                "(:id, :parent_job_id, :node_id, :kind, :payload_digest, :payload, "
                ":authority_revision, :state, :current_attempt, :created_at, "
                ":updated_at)"
            ),
            {
                "id": "legacy-operation",
                "parent_job_id": "legacy-job",
                "node_id": "legacy-node",
                "kind": "agent.upgrade.v1",
                "payload_digest": "2" * 64,
                "payload": "{}",
                "authority_revision": "legacy-authority",
                "state": "failed",
                "current_attempt": 1,
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
        connection.execute(
            text(
                "INSERT INTO agent_upgrade_compatibility_recoveries "
                "(id, node_id, job_id, operation_id, source_attempt, source_fence, "
                "source_certificate_serial, expected_retry_attempt, "
                "source_semantic_version, source_build_digest, source_binary_digest, "
                "upgrade_payload_sha256, package_sha256, target_package_version, "
                "target_build_digest, target_binary_digest, authority_revision, "
                "plan_digest, state, actor, request_id, created_at, completed_at, "
                "blocked_at, abandoned_at) "
                "VALUES "
                "(:id, :node_id, :job_id, :operation_id, :source_attempt, "
                ":source_fence, :source_certificate_serial, :expected_retry_attempt, "
                ":source_semantic_version, :source_build_digest, "
                ":source_binary_digest, :upgrade_payload_sha256, :package_sha256, "
                ":target_package_version, :target_build_digest, "
                ":target_binary_digest, :authority_revision, :plan_digest, :state, "
                ":actor, :request_id, :created_at, :completed_at, :blocked_at, "
                ":abandoned_at)"
            ),
            {
                "id": "legacy-recovery",
                "node_id": "legacy-node",
                "job_id": "legacy-job",
                "operation_id": "legacy-operation",
                "source_attempt": 1,
                "source_fence": "legacy-source-fence",
                "source_certificate_serial": "legacy-certificate",
                "expected_retry_attempt": 2,
                "source_semantic_version": "0.1.0",
                "source_build_digest": f"sha256:{'3' * 64}",
                "source_binary_digest": "4" * 64,
                "upgrade_payload_sha256": "5" * 64,
                "package_sha256": "6" * 64,
                "target_package_version": "0.1.1",
                "target_build_digest": f"sha256:{'7' * 64}",
                "target_binary_digest": "8" * 64,
                "authority_revision": "legacy-authority",
                "plan_digest": "9" * 64,
                "state": "abandoned",
                "actor": "migration-regression",
                "request_id": "legacy-recovery-request",
                "created_at": timestamp,
                "completed_at": timestamp,
                "blocked_at": timestamp,
                "abandoned_at": timestamp,
            },
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
                == "0020_runtime_image_receipts"
        )
        assert "agent_upgrade_compatibility_recoveries" in set(
            inspect(connection).get_table_names()
        )
        assert connection.execute(
            text(
                "SELECT id, state, plan_digest "
                "FROM agent_upgrade_compatibility_recoveries WHERE id = :id"
            ),
            {"id": "legacy-recovery"},
        ).one() == ("legacy-recovery", "abandoned", "9" * 64)


def test_alembic_autogenerate_ignores_only_retained_legacy_storage(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'autogenerate.sqlite'}"
    config = _config(url)
    command.upgrade(config, "head")

    migrations = tmp_path / "migrations"
    shutil.copytree(Path(__file__).resolve().parents[1] / "migrations", migrations)
    config.set_main_option("script_location", str(migrations))
    revision = command.revision(
        config,
        message="verify retained legacy exclusion",
        autogenerate=True,
        rev_id="retained_legacy_guard",
    )

    assert revision is not None
    generated = Path(revision.path).read_text()
    assert "agent_upgrade_compatibility_recoveries" not in generated
    assert "op.drop_table" not in generated
    assert "op.drop_index" not in generated


def test_recipe_installation_model_identity_migration_adds_indexed_nullable_column(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'model-identity-upgrade.sqlite'}"
    config = _config(url)
    command.upgrade(config, "0010_managed_recipe_catalog_sync")
    engine = create_engine(url)

    command.upgrade(config, "head")

    inspector = inspect(engine)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("recipe_installations")
    }
    assert columns["model_version_sha256"]["nullable"] is True
    assert "ix_recipe_installations_model_version_sha256" in {
        index["name"] for index in inspector.get_indexes("recipe_installations")
    }


def test_recipe_run_generation_migration_adds_exact_observation_authority(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'recipe-run-generation-upgrade.sqlite'}"
    config = _config(url)
    command.upgrade(config, "0011_recipe_model_identity")
    engine = create_engine(url)

    command.upgrade(config, "head")

    inspector = inspect(engine)
    run_columns = {
        column["name"]: column for column in inspector.get_columns("recipe_runs")
    }
    assert run_columns["run_generation"]["nullable"] is False
    assert run_columns["run_generation"]["default"] in {"'1'", "1"}
    assert "ck_recipe_runs_run_generation" in {
        constraint["name"]
        for constraint in inspector.get_check_constraints("recipe_runs")
    }
    grant_columns = {
        column["name"]: column
        for column in inspector.get_columns("recipe_run_observation_grants")
    }
    assert set(grant_columns) == {
        "run_node_id",
        "request_id",
        "identity_sha256",
        "issued_at",
        "expires_at",
        "consumed",
    }
    assert grant_columns["run_node_id"]["nullable"] is False
    assert grant_columns["consumed"]["nullable"] is False
    assert {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(
            "recipe_run_observation_grants"
        )
    } == {("request_id",)}


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
                == "0020_runtime_image_receipts"
        )


def test_existing_database_missing_fleet_profile_tables_is_repaired(
    tmp_path: Path,
) -> None:
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
        assert _metadata_differences_without_retained_legacy(connection) == []
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
                == "0020_runtime_image_receipts"
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


def test_fresh_baseline_is_forward_only_and_canonical(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'control.sqlite'}"
    config = _config(url)
    command.upgrade(config, "head")

    tables = set(inspect(create_engine(url)).get_table_names())
    assert "local_recipes" not in tables
    assert "local_recipe_revisions" not in tables
    assert "catalog_documents" in tables
    assert "runtime_image_receipts" in tables
