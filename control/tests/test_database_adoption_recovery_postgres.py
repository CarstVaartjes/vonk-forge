from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool, QueuePool
from vonk_control import db
from vonk_control.models import Base, CatalogDocumentRevision

from .test_exact_integer_storage import (
    _document,
    _historical_draft,
    _schema,
    legacy_engine,
)

__all__ = ["legacy_engine"]

pytestmark = pytest.mark.postgres
CONFIG = Path(__file__).resolve().parents[1] / "alembic.ini"


@pytest.fixture
def storage_engine(postgres_engine: Engine) -> Engine:
    """Reuse the actual historical fixture with PostgreSQL as its sole engine."""
    Base.metadata.create_all(postgres_engine)
    return postgres_engine


@dataclass(frozen=True)
class StartupDatabase:
    engine: Engine
    revision_id: str
    url: str


@pytest.fixture
def startup_database(legacy_engine: Engine) -> StartupDatabase:
    revision = _historical_draft(legacy_engine, _document(9, "startup-retained"))
    url = legacy_engine.url.render_as_string(hide_password=False)
    config = Config(str(CONFIG))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    # A real historical database records the existing baseline. Use the owning
    # Alembic command, so startup's upgrade still executes normally and does not
    # bootstrap tables while the intentional relation lock is held.
    command.stamp(config, "head")
    return StartupDatabase(legacy_engine, revision.id, url)


@dataclass
class Attempts:
    pids: dict[Engine, set[int]] = field(default_factory=dict)
    disposed: set[Engine] = field(default_factory=set)
    migration_pids: set[int] = field(default_factory=set)

    def assert_released(self, database: Engine) -> None:
        assert self.pids
        startup_engine = next(reversed(self.pids))
        assert startup_engine in self.disposed
        assert isinstance(startup_engine.pool, QueuePool)
        assert startup_engine.pool.checkedout() == 0
        for pid in self.pids[startup_engine] | self.migration_pids:
            with database.connect() as connection:
                assert (
                    connection.exec_driver_sql(
                        "SELECT count(*) FROM pg_locks WHERE pid=%s", (pid,)
                    ).scalar_one()
                    == 0
                )
                assert (
                    connection.exec_driver_sql(
                        "SELECT count(*) FROM pg_stat_activity WHERE pid=%s AND xact_start IS NOT NULL",
                        (pid,),
                    ).scalar_one()
                    == 0
                )


@contextmanager
def watch_native_attempts() -> Iterator[Attempts]:
    attempts = Attempts()
    captured_connections: set[Connection] = set()

    def before_sql(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        is_startup_owner = "pg_try_advisory_lock" in statement and "%(key)" in statement
        is_migration_connection = isinstance(connection.engine.pool, NullPool) and any(
            connection.engine.url == engine.url for engine in attempts.pids
        )
        if connection in captured_connections or (
            connection.engine not in attempts.pids
            and not is_startup_owner
            and not is_migration_connection
        ):
            return
        # Capture each native schema/advisory connection before its first SQL.
        # Mark before our PID query, which itself passes through this event.
        captured_connections.add(connection)
        pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
        assert isinstance(pid, int)
        if is_migration_connection:
            # Owning Alembic env.py closes its independent NullPool connection.
            # Verify its server ownership too; it is not an initialize engine
            # and therefore does not emit the initialize engine-disposed event.
            attempts.migration_pids.add(pid)
        else:
            attempts.pids.setdefault(connection.engine, set()).add(pid)

    def disposed(engine: Engine) -> None:
        if engine in attempts.pids:
            attempts.disposed.add(engine)

    event.listen(Engine, "before_cursor_execute", before_sql)
    event.listen(Engine, "engine_disposed", disposed)
    try:
        yield attempts
    finally:
        event.remove(Engine, "before_cursor_execute", before_sql)
        event.remove(Engine, "engine_disposed", disposed)


def observe_retry(
    monkeypatch: pytest.MonkeyPatch, *, timeout: float, sleep: Callable[[float], None]
) -> None:
    original = db.run_with_database_startup_retry

    def audited(operation: Callable[[], None], *, label: str = "database") -> None:
        # Only inject the existing timeout/sleep dependency seams. The actual
        # operation, native retry classification, fixed deadline and cleanup
        # remain the production implementation.
        original(operation, timeout_seconds=timeout, sleep=sleep, label=label)

    monkeypatch.setattr(db, "run_with_database_startup_retry", audited)


def assert_retained_and_current(database: StartupDatabase) -> None:
    with database.engine.begin() as connection:
        db.verify_schema_is_current(connection)
        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns("catalog_document_revisions")
        }
        assert str(columns["download_bytes"]["type"]) == "TEXT"
    sessions = sessionmaker(database.engine, expire_on_commit=False)
    with sessions() as session:
        revision = session.get(CatalogDocumentRevision, database.revision_id)
        assert revision is not None
        assert revision.download_bytes == revision.installed_bytes == 9


@pytest.mark.parametrize("owner", ["relation", "advisory"])
def test_initialize_database_same_call_releases_old_attempt_before_owner_clears(
    startup_database: StartupDatabase,
    owner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches retry requiring a second caller or sleeping with old SQL ownership."""
    database = startup_database
    with database.engine.connect() as blocker, watch_native_attempts() as attempts:
        if owner == "relation":
            blocker.exec_driver_sql(
                "LOCK TABLE catalog_document_revisions IN ACCESS EXCLUSIVE MODE"
            )
        else:
            assert (
                blocker.exec_driver_sql(
                    "SELECT pg_try_advisory_lock(%s)", (db._STARTUP_ADVISORY_LOCK,)
                ).scalar_one()
                is True
            )
            blocker.commit()
        waits: list[float] = []

        def release_before_backoff(wait: float) -> None:
            attempts.assert_released(database.engine)
            waits.append(wait)
            assert len(waits) == 1
            if owner == "relation":
                # The former attempt must have released its advisory owner
                # even while this separate relation owner remains active.
                with database.engine.connect() as probe:
                    assert (
                        probe.exec_driver_sql(
                            "SELECT pg_try_advisory_lock(%s)",
                            (db._STARTUP_ADVISORY_LOCK,),
                        ).scalar_one()
                        is True
                    )
                    assert (
                        probe.exec_driver_sql(
                            "SELECT pg_advisory_unlock(%s)",
                            (db._STARTUP_ADVISORY_LOCK,),
                        ).scalar_one()
                        is True
                    )
                    probe.commit()
                blocker.rollback()
            else:
                assert (
                    blocker.exec_driver_sql(
                        "SELECT pg_advisory_unlock(%s)", (db._STARTUP_ADVISORY_LOCK,)
                    ).scalar_one()
                    is True
                )
                blocker.commit()
            time.sleep(wait)

        observe_retry(monkeypatch, timeout=5, sleep=release_before_backoff)
        try:
            # Exactly one public entry call must finish migration and adoption.
            db.initialize_database(database.url, config_path=CONFIG)
            assert len(waits) == 1 and len(attempts.pids) == 2
            assert attempts.disposed == set(attempts.pids)
        finally:
            blocker.rollback()
            if owner == "advisory":
                blocker.exec_driver_sql(
                    "SELECT pg_advisory_unlock(%s)", (db._STARTUP_ADVISORY_LOCK,)
                )
                blocker.commit()
    assert_retained_and_current(database)


def test_initialize_database_busy_owner_exhausts_one_fixed_deadline_without_mutation(
    startup_database: StartupDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches contention renewing its deadline or retaining SQL between attempts."""
    database = startup_database
    before = _schema(database.engine)
    waits: list[float] = []
    with database.engine.connect() as blocker, watch_native_attempts() as attempts:
        assert (
            blocker.exec_driver_sql(
                "SELECT pg_try_advisory_lock(%s)", (db._STARTUP_ADVISORY_LOCK,)
            ).scalar_one()
            is True
        )
        blocker.commit()

        def bounded_backoff(wait: float) -> None:
            attempts.assert_released(database.engine)
            waits.append(wait)
            time.sleep(wait)

        observe_retry(monkeypatch, timeout=0.3, sleep=bounded_backoff)
        started = time.monotonic()
        try:
            with pytest.raises(RuntimeError, match="owner is busy"):
                db.initialize_database(database.url, config_path=CONFIG)
            assert waits and sum(waits) <= 0.3
            assert 0.3 <= time.monotonic() - started < 2
            assert attempts.disposed == set(attempts.pids)
        finally:
            blocker.exec_driver_sql(
                "SELECT pg_advisory_unlock(%s)", (db._STARTUP_ADVISORY_LOCK,)
            )
            blocker.commit()
    assert _schema(database.engine) == before


def test_initialize_database_unreviewed_schema_is_fatal_without_backoff(
    startup_database: StartupDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches treating a genuine schema integrity refusal as transient startup."""
    database = startup_database
    with database.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE INDEX proof_unreviewed_numeric ON catalog_document_revisions(download_bytes)"
        )
    before = _schema(database.engine)

    def forbidden_backoff(wait: float) -> None:
        raise AssertionError("schema integrity refusal was retried")

    observe_retry(monkeypatch, timeout=1, sleep=forbidden_backoff)
    with watch_native_attempts() as attempts:
        with pytest.raises(ValueError, match="unreviewed numeric"):
            db.initialize_database(database.url, config_path=CONFIG)
        assert len(attempts.pids) == 1
        attempts.assert_released(database.engine)
    assert _schema(database.engine) == before


def test_initialize_database_native_schema_permission_denial_is_fatal_without_backoff(
    startup_database: StartupDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches retrying actual SQLSTATE42501 or weakening schema authorization."""
    database = startup_database
    role = "proof_denied_" + uuid.uuid4().hex
    password = uuid.uuid4().hex
    before = _schema(database.engine)
    with database.engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        connection.exec_driver_sql("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        # Resolve the existing schema/version table normally, then let the
        # actual Alembic SELECT fail on table authorization with SQLSTATE42501.
        connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
        connection.exec_driver_sql(
            f'GRANT CONNECT ON DATABASE "{database.engine.url.database}" TO {role}'
        )

    def forbidden_backoff(wait: float) -> None:
        raise AssertionError("native schema authorization denial was retried")

    observe_retry(monkeypatch, timeout=1, sleep=forbidden_backoff)
    denied_url = database.engine.url.set(
        username=role, password=password
    ).render_as_string(hide_password=False)
    try:
        with watch_native_attempts() as attempts:
            with pytest.raises((DBAPIError, RuntimeError)) as failure:
                db.initialize_database(denied_url, config_path=CONFIG)
            cause = (
                failure.value
                if isinstance(failure.value, DBAPIError)
                else failure.value.__cause__
            )
            assert isinstance(cause, DBAPIError)
            assert getattr(cause.orig, "sqlstate", None) == "42501"
            assert len(attempts.pids) == 1
            attempts.assert_released(database.engine)
        assert _schema(database.engine) == before
    finally:
        with database.engine.begin() as connection:
            connection.exec_driver_sql(f"DROP OWNED BY {role}")
            connection.exec_driver_sql(f"DROP ROLE {role}")


def test_initialize_database_bad_handshake_is_bounded_unknown_with_unchanged_credentials(
    startup_database: StartupDatabase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catches claiming no-state auth recovery, changing credentials or unbounded retry."""
    database = startup_database
    before = _schema(database.engine)
    bad_password = uuid.uuid4().hex
    bad_url = database.engine.url.set(password=bad_password)
    failed: list[Engine] = []
    connected: list[Engine] = []
    waits: list[float] = []

    def connected_attempt(connection: Connection) -> None:
        if connection.engine.url == bad_url:
            connected.append(connection.engine)

    def disposed_attempt(engine: Engine) -> None:
        if engine.url == bad_url:
            failed.append(engine)
            assert isinstance(engine.pool, QueuePool)
            assert engine.pool.checkedout() == 0

    def bounded_unknown(wait: float) -> None:
        assert failed and not connected
        assert all(engine.url == bad_url for engine in failed)
        waits.append(wait)
        time.sleep(wait)

    observe_retry(monkeypatch, timeout=0.3, sleep=bounded_unknown)
    event.listen(Engine, "engine_connect", connected_attempt)
    event.listen(Engine, "engine_disposed", disposed_attempt)
    started = time.monotonic()
    try:
        with pytest.raises(OperationalError) as failure:
            db.initialize_database(
                bad_url.render_as_string(hide_password=False), config_path=CONFIG
            )
        assert getattr(failure.value.orig, "sqlstate", None) is None
        assert failed and waits and not connected
        assert all(engine.url == bad_url for engine in failed)
        assert sum(waits) <= 0.3
        assert 0.3 <= time.monotonic() - started < 2
    finally:
        event.remove(Engine, "engine_connect", connected_attempt)
        event.remove(Engine, "engine_disposed", disposed_attempt)
    diagnostics = capsys.readouterr().err
    assert "cause unknown" in diagnostics and "deadline expired" in diagnostics
    assert "credentials unchanged" in diagnostics and bad_password not in diagnostics
    assert _schema(database.engine) == before
    with database.engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=%s AND application_name='vonk:control'",
                (database.engine.url.database,),
            ).scalar_one()
            == 0
        )
