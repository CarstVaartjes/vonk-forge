from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from vonk_control.schema_bootstrap import bootstrap_schema, reset_schema, schema_sql


EXPECTED_TABLES = (
    "agent_nodes",
    "agent_profiles",
    "enrollment_intents",
    "enrollment_evidence",
    "certificate_records",
    "presence_snapshots",
    "telemetry_snapshots",
    "inventory_snapshots",
    "recipes",
    "recipe_revisions",
    "recipe_import_reports",
    "placements",
    "installations",
    "runs",
    "operations",
)


def _database_url() -> str:
    return os.environ.get("VONK_TEST_DATABASE_URL", "postgresql+psycopg://control@localhost/control")


def _postgres_engine():
    engine = create_engine(_database_url())
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as error:
        engine.dispose()
        pytest.skip(f"PostgreSQL test database unavailable: {error}")
    return engine


def test_checked_in_schema_is_direct_sql() -> None:
    path = Path(__file__).resolve().parents[1] / "schema" / "0001_initial.sql"
    assert path.read_text(encoding="utf-8") == schema_sql()
    assert "CREATE TABLE agent_nodes" in schema_sql()
    assert "CREATE TABLE operations" in schema_sql()


def test_fresh_schema_has_exact_authority_tables_and_is_empty() -> None:
    engine = _postgres_engine()
    try:
        reset_schema(engine)
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == set(EXPECTED_TABLES)
        with engine.connect() as connection:
            for table in EXPECTED_TABLES:
                assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
    finally:
        engine.dispose()


def test_fresh_schema_has_authority_tables_and_empty_fleet() -> None:
    engine = _postgres_engine()
    try:
        reset_schema(engine)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM agent_nodes")).scalar_one() == 0
            assert connection.execute(
                text("SELECT count(*) FROM agent_nodes WHERE state <> 'revoked'")
            ).scalar_one() == 0
    finally:
        engine.dispose()

def test_schema_reset_clears_seeded_dependent_rows() -> None:
    engine = _postgres_engine()
    try:
        reset_schema(engine)
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO agent_nodes (node_id, identity) VALUES ('node-1', 'identity-1')"))
            connection.execute(text("INSERT INTO agent_profiles (node_id, display_name) VALUES ('node-1', 'Node 1')"))
            connection.execute(text("""
                INSERT INTO enrollment_intents (intent_id, node_id, created_by, expires_at)
                VALUES ('intent-1', 'node-1', 'test', now() + interval '1 hour')
            """))
            connection.execute(text("""
                INSERT INTO enrollment_evidence
                    (evidence_id, intent_id, node_id, csr_pem, host_identity,
                     hardware_identity, agent_version, boot_id)
                VALUES ('evidence-1', 'intent-1', 'node-1', 'csr', 'host', 'hardware', '1', 'boot')
            """))
            connection.execute(text("INSERT INTO recipes (recipe_id, slug, title, source, created_by) VALUES ('recipe-1', 'recipe-1', 'Recipe 1', 'test', 'test')"))
            connection.execute(text("""
                INSERT INTO recipe_revisions
                    (revision_id, recipe_id, revision_number, content, content_digest, created_by)
                VALUES ('revision-1', 'recipe-1', 1, '{}', 'digest-1', 'test')
            """))
            connection.execute(text("INSERT INTO placements (placement_id, recipe_revision_id, node_id) VALUES ('placement-1', 'revision-1', 'node-1')"))
            connection.execute(text("""
                INSERT INTO installations
                    (installation_id, placement_id, recipe_revision_id, node_id, digest)
                VALUES ('installation-1', 'placement-1', 'revision-1', 'node-1', 'digest-1')
            """))
            connection.execute(text("INSERT INTO runs (run_id, installation_id, node_id) VALUES ('run-1', 'installation-1', 'node-1')"))
            connection.execute(text("INSERT INTO operations (operation_id, owner_kind, owner_id, node_id, kind) VALUES ('operation-1', 'run', 'run-1', 'node-1', 'test')"))
        reset_schema(engine)
        with engine.connect() as connection:
            for table in EXPECTED_TABLES:
                assert connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
    finally:
        engine.dispose()


def test_schema_reset_is_repeatable_and_constraints_are_present() -> None:
    engine = _postgres_engine()
    try:
        reset_schema(engine)
        bootstrap_schema(engine, reset=True)
        inspector = inspect(engine)
        indexes = {
            index["name"]
            for table in EXPECTED_TABLES
            for index in inspector.get_indexes(table)
        }
        assert "uq_active_certificate_identity" in indexes
        assert "uq_active_grant_per_intent" in indexes
        revision_uniques = inspector.get_unique_constraints("recipe_revisions")
        assert any(
            set(constraint.get("column_names") or []) == {"recipe_id", "revision_number"}
            for constraint in revision_uniques
        )
        evidence_fks = inspector.get_foreign_keys("enrollment_evidence")
        assert any(
            fk.get("referred_table") == "enrollment_intents"
            and fk.get("constrained_columns") == ["intent_id", "node_id"]
            and fk.get("referred_columns") == ["intent_id", "node_id"]
            for fk in evidence_fks
        )
    finally:
        engine.dispose()
