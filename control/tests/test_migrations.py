from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from vonk_control.models import Base, Job

ROOT = Path(__file__).resolve().parents[1]
LEGACY_TABLES = {
    "agent_upgrade_compatibility_recoveries",
    "local_recipe_revisions",
    "managed_recipe_library_links",
}


def _config(database_url: str) -> Config:
    config = Config(ROOT / "alembic.ini")
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _upgrade(database_url: str) -> None:
    command.upgrade(_config(database_url), "head")


def _schema_tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def _assert_current_schema(engine) -> None:
    assert _schema_tables(engine) == set(Base.metadata.tables)
    assert not LEGACY_TABLES & _schema_tables(engine)
    with engine.connect() as connection:
        assert list(
            MigrationContext.configure(connection).get_current_heads()
        ) == ["0000_fresh_schema"]
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "0000_fresh_schema"
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


def test_existing_database_fails_closed_with_disposable_reset_guidance(
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
        with pytest.raises(
            RuntimeError,
            match="not compatible.*No migration.*automatic drop.*disposable development",
        ):
            _upgrade(url)
        assert _schema_tables(engine) == {"agent_upgrade_compatibility_recoveries"}
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
