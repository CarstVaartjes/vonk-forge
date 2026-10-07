from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Collection, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DefaultClause,
    MetaData,
    Text,
    create_engine,
    event,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, StatementError
from sqlalchemy.orm import Session, sessionmaker
from vonk_control import exact_integer_adoption as adoption_module
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.catalog_revision_contract import (
    ModelRevisionProjection,
    read_catalog_projection,
)
from vonk_control.exact_integer_adoption import (
    adopt_exact_integer_columns,
    reconcile_exact_integer_schema,
)
from vonk_control.exact_integer_storage import DecimalIntegerOrderKey
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_contract import (
    CacheEntryResponse,
    ModelCacheOperationProgress,
)
from vonk_control.models import Base, CatalogDocumentRevision, ModelCacheSet
from vonk_control.recipe_action_plans import UninstallPlan
from vonk_control.unused_storage_collection import UnusedStorageCollector
from vonk_forge_contracts import ModelDefinition

from .test_catalog_entities import _model
from .test_model_cache import _artifact

NOW = datetime(2026, 10, 7, tzinfo=UTC)
VALUES = (0, 9, 10, 2**63 - 1, 2**63, 10**199 + 123)
OWNING_COLUMNS = {
    "catalog_document_revisions": ("download_bytes", "installed_bytes"),
    "model_cache_sets": ("expected_bytes", "verified_bytes"),
}
OWNING_CHECKS = {
    "ck_catalog_document_revision_download_bytes",
    "ck_catalog_document_revision_installed_bytes",
    "ck_model_cache_sets_sizes",
}


class NoRemoval:
    """Read-only pressure proof must never invoke an external removal."""

    def preview_uninstall(
        self, installation_id: str, *, also_removing: Collection[str] = ()
    ) -> UninstallPlan:
        raise AssertionError("pressure observation attempted an uninstall preview")

    def uninstall(
        self,
        installation_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        unattended_guard: Callable[[Session], None] | None = None,
        also_removing: Collection[str] = (),
    ) -> object:
        raise AssertionError("pressure observation attempted an uninstall")


def _document(size: int, slug: str) -> dict[str, object]:
    document = _model()
    identity = document["identity"]
    assert isinstance(identity, dict)
    identity["slug"] = slug
    files = document["files"]
    assert isinstance(files, list) and files
    first = files[0]
    assert isinstance(first, dict)
    first["size_bytes"] = size
    first.pop("parts", None)
    document["files"] = [first]
    ModelDefinition.model_validate_json(json.dumps(document))
    return document


def _adopt(engine: Engine) -> None:
    if engine.dialect.name == "sqlite":
        reconcile_exact_integer_schema(engine)
    else:
        with engine.begin() as connection:
            adopt_exact_integer_columns(connection)


@pytest.fixture
def legacy_engine(storage_engine: Engine, request: pytest.FixtureRequest) -> Engine:
    """Actual historical owning BIGINT schema; current non-owning metadata."""
    Base.metadata.drop_all(storage_engine)
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    for name, columns in OWNING_COLUMNS.items():
        table = metadata.tables[name]
        for column in columns:
            table.c[column].type = BigInteger()
        if (
            name == "catalog_document_revisions"
            and getattr(getattr(request.node, "callspec", None), "params", {}).get(
                "extension"
            )
            == "server-default"
        ):
            table.c.download_bytes.server_default = DefaultClause(text("9"))
        for constraint in tuple(table.constraints):
            if constraint.name in OWNING_CHECKS:
                table.constraints.remove(constraint)
        expression = (
            "expected_bytes >= 0 AND verified_bytes >= 0 AND verified_bytes <= expected_bytes"
            if name == "model_cache_sets"
            else "download_bytes >= 0 AND installed_bytes >= 0"
        )
        table.append_constraint(
            CheckConstraint(
                expression,
                name=next(
                    name
                    for name in OWNING_CHECKS
                    if name.startswith("ck_model_cache_sets")
                )
                if name == "model_cache_sets"
                else "ck_catalog_document_revision_download_bytes",
            )
        )
        table.append_constraint(
            CheckConstraint(
                "length(created_by) > 0"
                if name == "catalog_document_revisions"
                else "schema_version = 2",
                name=f"proof_named_{name}",
            )
        )
        table.append_constraint(
            CheckConstraint(
                "length(created_by) < 1024"
                if name == "catalog_document_revisions"
                else "schema_version <> 0"
            )
        )
    metadata.create_all(storage_engine)
    if storage_engine.dialect.name == "sqlite":
        with storage_engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
            connection.exec_driver_sql("PRAGMA busy_timeout=137")
            connection.commit()
    return storage_engine


def _schema(engine: Engine) -> tuple[tuple[object, ...], ...]:
    """Native schema/data snapshot for refusal-before-mutation assertions."""
    with engine.connect() as connection:
        if engine.dialect.name == "sqlite":
            return tuple(
                tuple(row)
                for row in connection.exec_driver_sql(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
                )
            )
        return (
            tuple(
                tuple(row)
                for row in connection.exec_driver_sql(
                    "SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod), "
                    "co.collname,pg_get_expr(d.adbin,d.adrelid) "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "JOIN pg_attribute a ON a.attrelid=c.oid "
                    "LEFT JOIN pg_collation co ON co.oid=a.attcollation "
                    "LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum "
                    "WHERE n.nspname='public' AND a.attnum>0 AND NOT a.attisdropped "
                    "ORDER BY c.relname,a.attnum"
                )
            )
            + tuple(
                tuple(row)
                for row in connection.exec_driver_sql(
                    "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE connamespace='public'::regnamespace ORDER BY conname"
                )
            )
            + tuple(
                tuple(row)
                for row in connection.exec_driver_sql(
                    "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname"
                )
            )
            + tuple(
                tuple(row)
                for row in connection.exec_driver_sql(
                    "SELECT viewname,definition FROM pg_views WHERE schemaname='public' ORDER BY viewname"
                )
            )
            + tuple(
                tuple(row)
                for row in connection.exec_driver_sql(
                    "SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid),pg_get_functiondef(t.tgfoid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname"
                )
            )
            + tuple(
                (table, *tuple(row))
                for table in OWNING_COLUMNS
                for row in connection.exec_driver_sql(
                    f"SELECT * FROM {table} ORDER BY 1"
                )
            )
        )


def test_actual_bigint_counterexample_then_native_adoption_and_same_ids(
    legacy_engine: Engine,
    tmp_path: Path,
) -> None:
    """Catches unchanged BIGINT/REAL storage and migration replacing identities."""
    engine = legacy_engine
    sessions = sessionmaker(engine, expire_on_commit=False)
    owner = CatalogEntityService(sessions, clock=lambda: NOW)
    existing = owner.create_draft(_document(9, "legacy-nine"), actor="operator")
    # The baseline attempt runs through the real producer, inside an isolated
    # outer transaction. Restore even if SQLite accepted a lossy REAL value.
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            baseline_sessions = sessionmaker(
                connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            )
            baseline = CatalogEntityService(baseline_sessions, clock=lambda: NOW)
            with pytest.raises((DBAPIError, ValueError)):
                row = baseline.create_draft(
                    _document(2**63, "baseline-large"), actor="operator"
                )
                with baseline_sessions() as session:
                    reread = session.get(CatalogDocumentRevision, row.id)
                    assert reread is not None
                    assert reread.download_bytes == 2**63
        finally:
            transaction.rollback()
    with sessions() as session:
        assert list(session.scalars(select(CatalogDocumentRevision.id))) == [
            existing.id
        ]
    _adopt(engine)
    _adopt(engine)  # Restart/idempotent startup must not recreate rows.
    with sessions() as session:
        retained = session.get(CatalogDocumentRevision, existing.id)
        assert retained is not None and retained.download_bytes == 9
    for value in (2**63, VALUES[-1]):
        row = owner.create_draft(
            _document(value, f"adopted-{len(str(value))}"), actor="operator"
        )
        with sessions() as session:
            retained = session.get(CatalogDocumentRevision, row.id)
            assert retained is not None and retained.download_bytes == value
    with engine.connect() as connection:
        for table, names in OWNING_COLUMNS.items():
            columns = {
                column["name"]: column
                for column in inspect(connection).get_columns(table)
            }
            assert all(isinstance(columns[name]["type"], Text) for name in names)


def _seed_cache(engine: Engine, root: Path, expected: int = 10) -> str:
    sessions = sessionmaker(engine, expire_on_commit=False)
    cache = ModelCacheService(
        sessions, root, fixture_sources=True, reserve_bytes=0, clock=lambda: NOW
    )
    try:
        artifact = _artifact(root.parent, b"proof", artifact_id=f"proof{expected}")
        artifact["download_bytes"] = expected
        manifest = cache.resolve_artifact_set(artifacts=[artifact])
        # The same native preparation entry used by start_download; no copied
        # ORM row or manually assembled manifest/DTO can hide a storage failure.
        with sessions.begin() as session:
            cache._ensure_set(session, manifest)
        return manifest.digest
    finally:
        cache.close()


def test_database_rejects_noncanonical_tokens_and_exact_adjacent_verified_bytes(
    storage_engine: Engine,
    tmp_path: Path,
) -> None:
    """Catches decimal coercion, lexical cross-field comparison and null-as-zero."""
    digest = _seed_cache(storage_engine, tmp_path / "cache", 10)
    for token in ("", "00", "01", "+1", "-1", "1.0", "1e2", "1\n", " 1", "١", "0x1"):
        with pytest.raises(StatementError), storage_engine.begin() as connection:
            connection.execute(
                ModelCacheSet.__table__.update()
                .where(ModelCacheSet.artifact_set_sha256 == digest)
                .values(expected_bytes=token)
            )
    # Direct SQL deliberately bypasses the Python decorator, proving the DB
    # owns canonicality too. Parameter spelling is native to each engine.
    parameter = "?" if storage_engine.dialect.name == "sqlite" else "%s"
    for token in ("", "00", "01", "+1", "-1", "1.0", "1e2", "1\n", " 1", "١"):
        with pytest.raises(DBAPIError), storage_engine.begin() as connection:
            connection.exec_driver_sql(
                f"UPDATE model_cache_sets SET expected_bytes={parameter} WHERE artifact_set_sha256={parameter}",
                (token, digest),
            )
    for value in (0, 9, 10):
        with storage_engine.begin() as connection:
            connection.execute(
                ModelCacheSet.__table__.update()
                .where(ModelCacheSet.artifact_set_sha256 == digest)
                .values(verified_bytes=value)
            )
    with pytest.raises(DBAPIError), storage_engine.begin() as connection:
        connection.execute(
            ModelCacheSet.__table__.update()
            .where(ModelCacheSet.artifact_set_sha256 == digest)
            .values(verified_bytes=11)
        )
    with pytest.raises(DBAPIError), storage_engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE model_cache_sets SET verified_bytes=NULL WHERE artifact_set_sha256={parameter}",
            (digest,),
        )
    with pytest.raises(StatementError), storage_engine.begin() as connection:
        connection.execute(
            ModelCacheSet.__table__.update()
            .where(ModelCacheSet.artifact_set_sha256 == digest)
            .values(verified_bytes=True)
        )
    with storage_engine.connect() as connection:
        assert connection.execute(
            select(ModelCacheSet.expected_bytes, ModelCacheSet.verified_bytes)
        ).one() == (10, 10)
    for expected in (0, 2**63 - 1, 2**63, VALUES[-1]):
        digest = _seed_cache(
            storage_engine, tmp_path / f"adjacent-{expected}", expected
        )
        for value in sorted({0, max(0, expected - 1), expected}):
            with storage_engine.begin() as connection:
                connection.execute(
                    ModelCacheSet.__table__.update()
                    .where(ModelCacheSet.artifact_set_sha256 == digest)
                    .values(verified_bytes=value)
                )
        with pytest.raises(DBAPIError), storage_engine.begin() as connection:
            connection.execute(
                ModelCacheSet.__table__.update()
                .where(ModelCacheSet.artifact_set_sha256 == digest)
                .values(verified_bytes=expected + 1)
            )
        with storage_engine.connect() as connection:
            assert connection.execute(
                select(
                    ModelCacheSet.expected_bytes, ModelCacheSet.verified_bytes
                ).where(ModelCacheSet.artifact_set_sha256 == digest)
            ).one() == (expected, expected)


def test_legacy_adoption_preserves_keys_extensions_and_checked_restart(
    legacy_engine: Engine,
    tmp_path: Path,
) -> None:
    """Catches table replacement losing foreign keys, CHECKs or unknown DDL."""
    engine = legacy_engine
    sessions = sessionmaker(engine, expire_on_commit=False)
    revision = CatalogEntityService(sessions, clock=lambda: NOW).create_draft(
        _document(9, "preserved-nine"), actor="operator"
    )
    digest = _seed_cache(engine, tmp_path / "cache", 9)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE proof_child (identity VARCHAR(64) PRIMARY KEY, revision_id VARCHAR(64) NOT NULL REFERENCES catalog_document_revisions(id), CONSTRAINT ck_proof_child_name CHECK (length(identity)>0), CHECK (length(revision_id)>0))"
        )
        parameter = "?" if engine.dialect.name == "sqlite" else "%s"
        connection.exec_driver_sql(
            f"INSERT INTO proof_child VALUES ({parameter},{parameter})",
            ("retained-child", revision.id),
        )
        connection.exec_driver_sql(
            "CREATE INDEX proof_revision_expression ON catalog_document_revisions (lower(created_by))"
        )
        connection.exec_driver_sql(
            "CREATE VIEW proof_revision_view AS SELECT id,created_by FROM catalog_document_revisions"
        )
        if engine.dialect.name == "sqlite":
            connection.exec_driver_sql(
                "CREATE TABLE proof_trigger_log (revision_id TEXT NOT NULL)"
            )
            connection.exec_driver_sql(
                "CREATE TRIGGER proof_revision_trigger AFTER UPDATE OF created_by ON catalog_document_revisions BEGIN INSERT INTO proof_trigger_log VALUES (NEW.id); END"
            )
        before_constraints = {
            table: (
                inspect(connection).get_pk_constraint(table),
                inspect(connection).get_foreign_keys(table),
                inspect(connection).get_unique_constraints(table),
            )
            for table in Base.metadata.tables
        }
    _adopt(engine)
    engine.dispose()  # New physical connections exercise actual startup state.
    _adopt(engine)
    with engine.begin() as connection:
        after_constraints = {
            table: (
                inspect(connection).get_pk_constraint(table),
                inspect(connection).get_foreign_keys(table),
                inspect(connection).get_unique_constraints(table),
            )
            for table in Base.metadata.tables
        }
        assert after_constraints == before_constraints
        checks = inspect(connection).get_check_constraints("catalog_document_revisions")
        assert any(
            check["name"] == "proof_named_catalog_document_revisions"
            for check in checks
        )
        assert any("1024" in str(check["sqltext"]) for check in checks)
        assert connection.exec_driver_sql(
            "SELECT identity,revision_id FROM proof_child"
        ).one() == ("retained-child", revision.id)
        assert (
            connection.exec_driver_sql(
                "SELECT id FROM proof_revision_view"
            ).scalar_one()
            == revision.id
        )
        connection.exec_driver_sql(
            "UPDATE catalog_document_revisions SET created_by='repaired-operator'"
        )
        if engine.dialect.name == "sqlite":
            assert (
                connection.exec_driver_sql(
                    "SELECT revision_id FROM proof_trigger_log"
                ).scalar_one()
                == revision.id
            )
            assert (
                connection.exec_driver_sql("PRAGMA foreign_key_check").first() is None
            )
            names = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).scalars()
            )
        else:
            names = set(
                connection.exec_driver_sql(
                    "SELECT indexname FROM pg_indexes WHERE schemaname='public'"
                ).scalars()
            )
        assert "proof_revision_expression" in names
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO proof_child VALUES ('','missing')")
    with sessions() as session:
        retained = session.get(CatalogDocumentRevision, revision.id)
        assert retained is not None and retained.download_bytes == 9
        cache_set = session.get(ModelCacheSet, digest)
        assert cache_set is not None and cache_set.expected_bytes == 9


@pytest.mark.parametrize(
    "extension",
    [
        "numeric-check",
        "numeric-index",
        "numeric-view",
        "column-collation",
        "server-default",
        "user-trigger",
        "external-trigger",
    ],
)
def test_unreviewed_numeric_extension_defers_without_schema_or_data_mutation(
    legacy_engine: Engine,
    extension: str,
) -> None:
    """Catches dropping or changing unreviewed numeric extension semantics."""
    engine = legacy_engine
    sessions = sessionmaker(engine, expire_on_commit=False)
    revision = CatalogEntityService(sessions, clock=lambda: NOW).create_draft(
        _document(9, "deferred-nine"), actor="operator"
    )
    with engine.begin() as connection:
        if extension == "numeric-check":
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql(
                    "ALTER TABLE catalog_document_revisions ADD CONSTRAINT proof_numeric_check CHECK (download_bytes < 100)"
                )
            else:
                # Add via the actual reflected legacy schema, not a rewritten
                # producer. SQLite cannot ALTER ADD a table CHECK.
                connection.exec_driver_sql(
                    "CREATE INDEX proof_numeric_check ON catalog_document_revisions(download_bytes) WHERE download_bytes < 100"
                )
        elif extension == "numeric-index":
            connection.exec_driver_sql(
                "CREATE INDEX proof_numeric_index ON catalog_document_revisions(download_bytes)"
            )
        elif extension == "numeric-view":
            connection.exec_driver_sql(
                "CREATE VIEW proof_numeric_view AS SELECT download_bytes FROM catalog_document_revisions"
            )
        elif extension == "server-default":
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql(
                    "ALTER TABLE catalog_document_revisions ALTER COLUMN download_bytes SET DEFAULT 9"
                )
        elif extension == "user-trigger":
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql(
                    "CREATE FUNCTION proof_user_trigger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
                )
                connection.exec_driver_sql(
                    "CREATE TRIGGER proof_user_trigger BEFORE UPDATE ON catalog_document_revisions FOR EACH ROW EXECUTE FUNCTION proof_user_trigger()"
                )
            else:
                connection.exec_driver_sql(
                    "CREATE TRIGGER proof_numeric_trigger BEFORE UPDATE OF download_bytes ON catalog_document_revisions BEGIN SELECT NEW.download_bytes; END"
                )
        elif extension == "external-trigger":
            connection.exec_driver_sql(
                "CREATE TABLE proof_external (identity TEXT PRIMARY KEY)"
            )
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql(
                    "CREATE FUNCTION proof_external_trigger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM download_bytes FROM catalog_document_revisions; RETURN NEW; END $$"
                )
                connection.exec_driver_sql(
                    "CREATE TRIGGER proof_external_trigger BEFORE INSERT ON catalog_document_revisions FOR EACH ROW EXECUTE FUNCTION proof_external_trigger()"
                )
            else:
                connection.exec_driver_sql(
                    "CREATE TRIGGER proof_external_trigger AFTER INSERT ON proof_external BEGIN SELECT download_bytes FROM catalog_document_revisions; END"
                )
        elif engine.dialect.name == "postgresql":
            connection.exec_driver_sql(
                'ALTER TABLE catalog_document_revisions ALTER COLUMN created_by TYPE TEXT COLLATE "C"'
            )
        else:
            connection.exec_driver_sql(
                "ALTER TABLE catalog_document_revisions ADD COLUMN proof_collated TEXT COLLATE NOCASE"
            )
    before = _schema(engine)
    with engine.connect() as connection:
        settings = (
            tuple(
                connection.exec_driver_sql(f"PRAGMA {key}").scalar_one()
                for key in ("foreign_keys", "legacy_alter_table", "busy_timeout")
            )
            if engine.dialect.name == "sqlite"
            else ()
        )
    # PostgreSQL ALTER preserves unrelated column collation; SQLite reflection
    # cannot, and must defer rather than erase it.
    if extension == "server-default" and engine.dialect.name == "sqlite":
        _adopt(engine)
        with engine.connect() as connection:
            columns = {
                column["name"]: column
                for column in inspect(connection).get_columns(
                    "catalog_document_revisions"
                )
            }
            assert columns["download_bytes"]["default"] == "9"
    elif extension == "column-collation" and engine.dialect.name == "postgresql":
        _adopt(engine)
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT collation_name FROM information_schema.columns WHERE table_name='catalog_document_revisions' AND column_name='created_by'"
                ).scalar_one()
                == "C"
            )
    else:
        with pytest.raises(ValueError, match="(deferred|unreviewed|dependent)"):
            _adopt(engine)
        assert _schema(engine) == before
    with engine.connect() as connection:
        if engine.dialect.name == "sqlite":
            assert (
                tuple(
                    connection.exec_driver_sql(f"PRAGMA {key}").scalar_one()
                    for key in ("foreign_keys", "legacy_alter_table", "busy_timeout")
                )
                == settings
            )
        assert connection.exec_driver_sql(
            "SELECT id,download_bytes FROM catalog_document_revisions"
        ).one() in ((revision.id, 9), (revision.id, "9"))


def test_real_schema_lock_conflict_releases_and_same_database_retry_converges(
    legacy_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches unbounded startup lock waits and poisoned retries after release."""
    engine = legacy_engine
    monkeypatch.setattr(
        adoption_module,
        "DATABASE_WAIT_BUDGETS",
        replace(
            adoption_module.DATABASE_WAIT_BUDGETS,
            lock_timeout_ms=100,
            transaction_timeout_ms=500,
        ),
    )
    before = _schema(engine)
    with engine.connect() as blocker:
        transaction = blocker.begin()
        if engine.dialect.name == "sqlite":
            blocker.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            blocker.exec_driver_sql(
                "LOCK TABLE catalog_document_revisions IN ACCESS EXCLUSIVE MODE"
            )
        started = time.monotonic()
        try:
            with pytest.raises(DBAPIError):
                _adopt(engine)
            assert time.monotonic() - started < 3
        finally:
            transaction.rollback()
    assert _schema(engine) == before
    _adopt(engine)
    with engine.connect() as connection:
        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("model_cache_sets")
        }
        assert isinstance(columns["expected_bytes"]["type"], Text)
        if engine.dialect.name == "sqlite":
            driver = connection.connection.driver_connection
            assert isinstance(driver, sqlite3.Connection)
            # A stale deadline callback would cancel ordinary post-retry SQL.
            assert (
                connection.exec_driver_sql(
                    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<2000) SELECT sum(x) FROM n"
                ).scalar_one()
                == 2001000
            )


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def storage_engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    if request.param == "postgres":
        engine = request.getfixturevalue("postgres_engine")
        assert isinstance(engine, Engine)
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'exact-integers.sqlite'}")
    Base.metadata.create_all(engine)
    yield engine
    if request.param == "sqlite":
        engine.dispose()


def test_native_catalog_and_cache_preserve_complete_integer_domain_across_restart(
    storage_engine: Engine, tmp_path: Path
) -> None:
    """BIGINT storage fails real draft/prepare; lexical max misorders 9 and 10."""
    sessions = sessionmaker(storage_engine, expire_on_commit=False)
    catalog = CatalogEntityService(sessions, clock=lambda: NOW)
    cache = ModelCacheService(
        sessions,
        tmp_path / "cache",
        fixture_sources=True,
        reserve_bytes=0,
        clock=lambda: NOW,
    )
    accepted = []
    try:
        for index, expected in enumerate(VALUES):
            document = _model()
            identity = document["identity"]
            assert isinstance(identity, dict)
            identity["slug"] = f"exact-integer-{index}"
            files = document["files"]
            assert isinstance(files, list) and files
            first = files[0]
            assert isinstance(first, dict)
            first["size_bytes"] = expected
            first.pop("parts", None)
            document["files"] = [first]
            native = ModelDefinition.model_validate_json(json.dumps(document))
            draft = catalog.create_draft(document, actor="operator")
            assert draft.download_bytes == native.download_bytes == expected
            assert draft.installed_bytes == native.installed_bytes == expected

            artifact = _artifact(
                tmp_path,
                f"source-{index}".encode(),
                artifact_id=f"weights{index}",
                model_content_sha256=draft.content_digest or "",
            )
            artifact["download_bytes"] = expected
            preview = cache.download_preview(artifacts=[artifact])
            request_key = str(uuid.uuid4())
            receipt = cache.start_download(
                actor="operator",
                request_key=request_key,
                plan_digest=str(preview["plan_digest"]),
                artifacts=[artifact],
            )
            progress = ModelCacheOperationProgress.model_validate_json(
                json.dumps(receipt.progress)
            )
            assert progress.measurement.total_bytes == expected
            manifest = cache.resolve_artifact_set(artifacts=[artifact])
            entry = CacheEntryResponse.model_validate_json(
                json.dumps(cache.get_entry(manifest.digest))
            )
            assert entry.expected_bytes == expected
            assert entry.verified_bytes == 0  # Preparation does not invent bytes.
            evidence = cache.preparation_evidence(manifest.digest)
            assert evidence["artifact_set_bytes"] == expected
            accepted.append(
                (draft.id, manifest.digest, receipt.id, request_key, expected)
            )
    finally:
        cache.close()

    restarted_catalog = CatalogEntityService(sessions, clock=lambda: NOW)
    restarted_cache = ModelCacheService(
        sessions,
        tmp_path / "cache",
        fixture_sources=True,
        reserve_bytes=0,
        clock=lambda: NOW,
    )
    try:
        with sessions() as session:
            for revision_id, digest, operation_id, request_key, expected in accepted:
                revision = session.get(CatalogDocumentRevision, revision_id)
                assert revision is not None
                assert revision.download_bytes == revision.installed_bytes == expected
                projection = read_catalog_projection(revision)
                assert isinstance(projection, ModelRevisionProjection)
                assert projection.download_bytes == expected
                entry = CacheEntryResponse.model_validate_json(
                    json.dumps(restarted_cache.get_entry(digest))
                )
                assert entry.expected_bytes == expected and entry.verified_bytes == 0
                operation = restarted_cache.get_operation(operation_id)
                assert (
                    operation.id == operation_id
                    and operation.request_key == request_key
                )
            maximum = session.scalar(
                select(ModelCacheSet.expected_bytes)
                .order_by(
                    func.length(ModelCacheSet.expected_bytes).desc(),
                    DecimalIntegerOrderKey(ModelCacheSet.expected_bytes).desc(),
                )
                .limit(1)
            )
            assert maximum == VALUES[-1]
        # Reconstructing the catalog owner must retain the same immutable row,
        # not replace it with a corrected/clamped revision.
        assert restarted_catalog is not catalog
        collector = UnusedStorageCollector(
            sessions,
            clock=lambda: NOW,
            lifecycle=NoRemoval(),
            model_cache_root=tmp_path / "cache",
            model_cache=restarted_cache,
            # Capacity observations are a supplied external dependency; the
            # SQL/native largest-set producer and pressure predicate are real.
            disk_usage=lambda _path: (10**12, 0),
            low_free_fraction=0,
            low_free_cap_fraction=1,
            reserve_fraction=0,
            reserve_floor_bytes=0,
        )
        pressures = collector._pressures(NOW)
        assert len(pressures) == 1
        assert pressures[0].shortfall == 10**12
        assert restarted_cache.unused_set_bytes(accepted[-1][1]) == 0
    finally:
        restarted_cache.close()


def test_invalid_legacy_row_refuses_before_mutation_then_exact_repair_retries(
    legacy_engine: Engine,
) -> None:
    """Catches malformed casts or partial adoption of another owning table."""
    engine = legacy_engine
    sessions = sessionmaker(engine, expire_on_commit=False)
    revision = CatalogEntityService(sessions, clock=lambda: NOW).create_draft(
        _document(9, "invalid-then-repaired"), actor="operator"
    )
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.exec_driver_sql(
                "ALTER TABLE catalog_document_revisions DROP CONSTRAINT ck_catalog_document_revision_download_bytes"
            )
            connection.exec_driver_sql(
                "UPDATE catalog_document_revisions SET download_bytes=-1"
            )
        else:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            try:
                connection.exec_driver_sql(
                    "UPDATE catalog_document_revisions SET download_bytes=-1"
                )
            finally:
                connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
            assert (
                connection.exec_driver_sql(
                    "PRAGMA ignore_check_constraints"
                ).scalar_one()
                == 0
            )
    before_schema = _schema(engine)
    sqlite_file = (
        Path(engine.url.database or "") if engine.dialect.name == "sqlite" else None
    )
    before_bytes = (
        hashlib.sha256(sqlite_file.read_bytes()).hexdigest() if sqlite_file else None
    )
    with pytest.raises(ValueError, match="invalid rows"):
        _adopt(engine)
    assert _schema(engine) == before_schema
    if sqlite_file:
        assert hashlib.sha256(sqlite_file.read_bytes()).hexdigest() == before_bytes
    with engine.begin() as connection:
        assert connection.exec_driver_sql(
            "SELECT id,download_bytes FROM catalog_document_revisions"
        ).one() == (revision.id, -1)
        connection.exec_driver_sql(
            "UPDATE catalog_document_revisions SET download_bytes=9"
        )
    _adopt(engine)
    with sessions() as session:
        repaired = session.get(CatalogDocumentRevision, revision.id)
        assert repaired is not None and repaired.download_bytes == 9


def test_current_schema_startup_does_not_take_sqlite_write_lock_or_change_settings(
    storage_engine: Engine,
) -> None:
    """Catches startup rebuilding already-current owner columns on every open."""
    statements: list[str] = []

    def capture(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(storage_engine, "before_cursor_execute", capture)
    try:
        _adopt(storage_engine)
    finally:
        event.remove(storage_engine, "before_cursor_execute", capture)
    assert not any(
        "ALTER TABLE" in sql.upper() or "BEGIN IMMEDIATE" in sql.upper()
        for sql in statements
    )
    if storage_engine.dialect.name == "sqlite":
        assert not any("PRAGMA" in sql.upper() and "=" in sql for sql in statements)


@pytest.mark.parametrize("option", ["STRICT", "WITHOUT ROWID", "AUTOINCREMENT"])
def test_sqlite_native_table_options_and_highwater_are_preserved_or_deferred(
    tmp_path: Path,
    option: str,
) -> None:
    """Catches reflection losing native table options or sequence high-water."""
    engine = create_engine(f"sqlite:///{tmp_path / 'options.sqlite'}")
    primary = (
        "sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        if option == "AUTOINCREMENT"
        else "artifact_set_sha256 TEXT PRIMARY KEY,"
    )
    suffix = "" if option == "AUTOINCREMENT" else option
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE TABLE model_cache_sets ({primary} expected_bytes INTEGER NOT NULL, verified_bytes INTEGER NOT NULL, CONSTRAINT ck_model_cache_sets_sizes CHECK(expected_bytes >= 0 AND verified_bytes >= 0 AND verified_bytes <= expected_bytes)) {suffix}"
            )
            if option == "AUTOINCREMENT":
                connection.exec_driver_sql(
                    "INSERT INTO model_cache_sets VALUES (100,9,0)"
                )
                connection.exec_driver_sql("DELETE FROM model_cache_sets")
                connection.exec_driver_sql(
                    "INSERT INTO model_cache_sets(expected_bytes,verified_bytes) VALUES(9,0)"
                )
            else:
                connection.exec_driver_sql(
                    "INSERT INTO model_cache_sets VALUES ('retained',9,0)"
                )
        before = _schema(engine)
        if option == "AUTOINCREMENT":
            with pytest.raises(ValueError, match="(AUTOINCREMENT|high.water|deferred)"):
                _adopt(engine)
            assert _schema(engine) == before
            with engine.connect() as connection:
                assert (
                    connection.exec_driver_sql(
                        "SELECT sequence_id FROM model_cache_sets"
                    ).scalar_one()
                    == 101
                )
                assert (
                    connection.exec_driver_sql(
                        "SELECT seq FROM sqlite_sequence WHERE name='model_cache_sets'"
                    ).scalar_one()
                    == 101
                )
        else:
            _adopt(engine)
            with engine.connect() as connection:
                ddl = connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE name='model_cache_sets'"
                ).scalar_one()
                assert isinstance(ddl, str) and option in ddl.upper()
                assert connection.exec_driver_sql(
                    "SELECT artifact_set_sha256,expected_bytes,verified_bytes FROM model_cache_sets"
                ).one() == ("retained", "9", "0")
    finally:
        engine.dispose()


def test_adoption_execution_deadline_rolls_back_partial_ddl_then_same_identity_retries(
    legacy_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches partial adoption escaping a real execution timeout, or poisoned retry."""
    engine = legacy_engine
    sessions = sessionmaker(engine, expire_on_commit=False)
    revision = CatalogEntityService(sessions, clock=lambda: NOW).create_draft(
        _document(9, "deadline-retained"), actor="operator"
    )
    digest = _seed_cache(engine, tmp_path / "deadline-cache", 9)
    monkeypatch.setattr(
        adoption_module,
        "DATABASE_WAIT_BUDGETS",
        replace(
            adoption_module.DATABASE_WAIT_BUDGETS,
            lock_timeout_ms=100,
            statement_timeout_ms=100,
            transaction_timeout_ms=2000,
        ),
    )
    before = _schema(engine)
    with engine.connect() as connection:
        settings = (
            tuple(
                connection.exec_driver_sql(f"PRAGMA {key}").scalar_one()
                for key in ("foreign_keys", "legacy_alter_table", "busy_timeout")
            )
            if engine.dialect.name == "sqlite"
            else (connection.exec_driver_sql("SHOW statement_timeout").scalar_one(),)
        )
    observed = {"partial_ddl": False}

    def delay_after_first_table(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if observed["partial_ddl"]:
            return
        sql = " ".join(statement.upper().replace('"', "").split())
        checkpoint = (
            "ALTER TABLE CATALOG_DOCUMENT_REVISIONS ALTER COLUMN INSTALLED_BYTES TYPE TEXT"
            in sql
            if engine.dialect.name == "postgresql"
            else sql.startswith(
                "ALTER TABLE _ALEMBIC_TMP_CATALOG_DOCUMENT_REVISIONS RENAME TO CATALOG_DOCUMENT_REVISIONS"
            )
        )
        if not checkpoint:
            return
        # Stop only after native migration made a real first-table change.
        # The second owning table must still have its old storage. This is
        # fault injection into the same database transaction, not a fake
        # migration implementation or a mocked exception.
        observed["partial_ddl"] = True
        first = {
            column["name"]: column
            for column in inspect(connection).get_columns("catalog_document_revisions")
        }
        second = {
            column["name"]: column
            for column in inspect(connection).get_columns("model_cache_sets")
        }
        assert all(
            isinstance(first[name]["type"], Text)
            for name in OWNING_COLUMNS["catalog_document_revisions"]
        )
        assert all(
            isinstance(second[name]["type"], BigInteger)
            for name in OWNING_COLUMNS["model_cache_sets"]
        )
        if engine.dialect.name == "postgresql":
            connection.exec_driver_sql("SET LOCAL statement_timeout='100ms'")
            connection.exec_driver_sql("SELECT pg_sleep(2)")
        else:
            # The owning startup callback, installed by native reconciliation,
            # interrupts this real SQL when its 2-second transaction budget
            # expires. No synthetic Python failure stands in for cancellation.
            connection.exec_driver_sql(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) SELECT sum(x) FROM n"
            )
        raise AssertionError("native execution deadline did not interrupt delayed SQL")

    event.listen(engine, "after_cursor_execute", delay_after_first_table)
    started = time.monotonic()
    try:
        with pytest.raises(DBAPIError) as failure:
            _adopt(engine)
        assert observed["partial_ddl"], (
            "fault never reached actual first-table mutation"
        )
        assert time.monotonic() - started < 6
        assert (
            "statement timeout" if engine.dialect.name == "postgresql" else "interrupt"
        ) in str(failure.value).lower()
    finally:
        event.remove(engine, "after_cursor_execute", delay_after_first_table)
    assert _schema(engine) == before
    with engine.connect() as connection:
        if engine.dialect.name == "sqlite":
            assert (
                tuple(
                    connection.exec_driver_sql(f"PRAGMA {key}").scalar_one()
                    for key in ("foreign_keys", "legacy_alter_table", "busy_timeout")
                )
                == settings
            )
            assert (
                connection.exec_driver_sql(
                    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<2000) SELECT sum(x) FROM n"
                ).scalar_one()
                == 2001000
            )
        else:
            assert (
                connection.exec_driver_sql("SHOW statement_timeout").scalar_one(),
            ) == settings
    _adopt(engine)
    _adopt(engine)
    with sessions() as session:
        retained = session.get(CatalogDocumentRevision, revision.id)
        retained_cache = session.get(ModelCacheSet, digest)
        assert (
            retained is not None
            and retained.download_bytes == retained.installed_bytes == 9
        )
        assert (
            retained_cache is not None
            and retained_cache.expected_bytes == 9
            and retained_cache.verified_bytes == 0
        )


def test_sqlite_weakened_owning_check_with_significant_literal_space_is_replaced(
    tmp_path: Path,
) -> None:
    """Catches whitespace/case normalization blessing a different CHECK literal."""
    engine = create_engine(f"sqlite:///{tmp_path / 'check-literal.sqlite'}")
    try:
        owning = next(
            constraint
            for constraint in Base.metadata.tables["model_cache_sets"].constraints
            if isinstance(constraint, CheckConstraint)
            and constraint.name == "ck_model_cache_sets_sizes"
        )
        current = str(owning.sqltext.compile(dialect=engine.dialect))
        weakened = current.replace("'0'", "'0 '", 1)
        assert current != weakened
        assert (
            "".join(current.split()).casefold() == "".join(weakened.split()).casefold()
        )
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE TABLE model_cache_sets (artifact_set_sha256 TEXT PRIMARY KEY,expected_bytes TEXT NOT NULL,verified_bytes TEXT NOT NULL,CONSTRAINT ck_model_cache_sets_sizes CHECK({weakened}))"
            )
            connection.exec_driver_sql(
                "INSERT INTO model_cache_sets VALUES('retained','9','0')"
            )
        _adopt(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("UPDATE model_cache_sets SET expected_bytes='0'")
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE model_cache_sets SET expected_bytes='1 '"
            )
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT expected_bytes,verified_bytes FROM model_cache_sets"
            ).one() == ("0", "0")
    finally:
        engine.dispose()
