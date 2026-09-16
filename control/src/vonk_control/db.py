"""Database engine, startup retry, and session construction."""

import sys
import time
from collections.abc import Callable
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError
from sqlalchemy.orm import Session, sessionmaker

from .settings import database_wait_budgets

_STARTUP_ADVISORY_LOCK = 8_241_779_103
_ALEMBIC_CONFIG = Path(__file__).resolve().parent / "alembic.ini"
_DATABASE_STARTUP_TIMEOUT_SECONDS = 120.0
_DATABASE_RETRYABLE_ERRORS = (InterfaceError, OperationalError, TimeoutError)


def build_engine(database_url: str) -> Engine:
    """Build the one engine every component shares.

    Configuration owns every finite wait budget; PostgreSQL receives all four
    server-side timeouts per connection, and its pool is explicitly bounded so
    a saturated pool fails within the pool timeout instead of queueing a
    connection forever. SQLite accepts neither the server options nor an
    explicit pool size, so it keeps the default pool.
    """

    budgets = database_wait_budgets()
    if "postgres" in database_url:
        connect_args = {
            "options": " ".join(
                (
                    f"-c lock_timeout={budgets.lock_timeout_ms}",
                    f"-c statement_timeout={budgets.statement_timeout_ms}",
                    f"-c transaction_timeout={budgets.transaction_timeout_ms}",
                    "-c idle_in_transaction_session_timeout="
                    + f"{budgets.idle_in_transaction_timeout_ms}",
                )
            )
        }
        return create_engine(
            database_url,
            pool_pre_ping=True,
            pool_size=budgets.pool_size,
            max_overflow=budgets.max_overflow,
            pool_timeout=budgets.pool_timeout_seconds,
            connect_args=connect_args,
        )
    return create_engine(database_url, pool_pre_ping=True, connect_args={})


def session_factory(engine: Engine) -> sessionmaker[Session]:
    from .fleet_events import FleetEventRecorder

    sessions = sessionmaker(engine, expire_on_commit=False)
    FleetEventRecorder.install(sessions)
    return sessions


def run_with_database_startup_retry[T](
    operation: Callable[[], T],
    *,
    timeout_seconds: float = _DATABASE_STARTUP_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    label: str = "database",
) -> T:
    """Retry transient database connection failures for a bounded interval.

    Container DNS and PostgreSQL can become available in either order after a
    host or Docker restart.  Keep startup deterministic by retrying only
    connection-class failures, logging a redacted diagnostic, and always
    re-raising once the fixed deadline expires.  Schema and permission errors
    remain fatal immediately.
    """
    if timeout_seconds < 0 or timeout_seconds > 900:
        raise ValueError("database startup timeout is outside the safe bound")
    deadline = monotonic() + timeout_seconds
    delay = 0.5
    attempts = 0
    while True:
        try:
            return operation()
        except _DATABASE_RETRYABLE_ERRORS as error:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise
            wait = min(delay, remaining)
            attempts += 1
            print(
                f"{label} unavailable during startup (attempt {attempts}; "
                f"{type(error).__name__}); retrying in {wait:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            sleep(wait)
            delay = min(delay * 2, 10.0)


def wait_for_database(database_url: str) -> None:
    """Wait for one bounded, authenticated PostgreSQL connection."""

    def connect_once() -> None:
        engine = build_engine(database_url)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()

    run_with_database_startup_retry(connect_once, label="PostgreSQL")


def upgrade_schema(
    database_url: str,
    *,
    config_path: Path = _ALEMBIC_CONFIG,
) -> None:
    """Create the current fresh schema, failing closed on existing tables."""
    if not database_url.strip():
        raise RuntimeError("database URL secret is empty")
    config = Config(str(config_path))
    # ConfigParser treats percent signs as interpolation markers.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


# ``alembic_version`` cannot prove that a live database is the current schema.
# The fresh baseline creates its tables from ``Base.metadata``, so a database
# created before a model change keeps the retired shape while already reporting
# ``0000_fresh_schema``; a removed column then stays behind ``NOT NULL`` and
# every insert fails long after startup.  Comparing the live catalog against the
# current metadata is the only check that can see it, so startup fails closed.
_SCHEMA_DIFFERENCE_LABELS = {
    "add_table": "missing table",
    "remove_table": "unexpected table",
    "add_column": "missing column",
    "remove_column": "unexpected column",
    "add_constraint": "missing constraint",
    "remove_constraint": "unexpected constraint",
    "add_index": "missing index",
    "remove_index": "unexpected index",
    "modify_nullable": "changed nullability",
    "modify_type": "changed type",
    "modify_default": "changed default",
}


# Reviewed spurious reports, keyed by ``(operation, object name)``.  The
# declared ``UniqueConstraint`` on ``model_cache_set_artifacts`` covers that
# table's two primary-key columns, so PostgreSQL realises the same uniqueness as
# the primary key under that name.  Uniqueness is enforced; only the
# constraint-kind comparison disagrees, and it disagrees on a freshly created
# schema, so it is not drift.  ``test_migrations`` pins the same report.
_TOLERATED_SCHEMA_DIFFERENCES = frozenset(
    {("add_constraint", "uq_model_cache_set_artifact_key")}
)
_SCHEMA_DIFFERENCE_LIMIT = 16
# Differences whose fourth element is a ``Column`` rather than a name.
_COLUMN_DIFFERENCE_OPERATIONS = frozenset(
    {
        "add_column",
        "remove_column",
        "modify_nullable",
        "modify_type",
        "modify_default",
    }
)


def _schema_difference_key(difference: tuple[object, ...]) -> tuple[str, str | None]:
    """Identify one autogenerate difference for the reviewed-tolerance check."""

    name: str | None = None
    for value in difference[1:]:
        candidate = getattr(value, "name", None)
        if isinstance(candidate, str):
            name = candidate
        elif isinstance(value, str):
            name = value
    return (str(difference[0]), name)


def _schema_difference_label(difference: tuple[object, ...]) -> str:
    """Name one difference so the operator knows exactly what to reconcile."""

    operation = str(difference[0])
    qualifier = _SCHEMA_DIFFERENCE_LABELS.get(operation, operation.replace("_", " "))
    if operation in _COLUMN_DIFFERENCE_OPERATIONS:
        # ``(operation, schema, table name, Column, ...)``: the column renders as
        # "table.column" by itself, so name the two parts explicitly.
        table_name = difference[2] if len(difference) > 2 else None
        column_name = getattr(
            difference[3] if len(difference) > 3 else None, "name", None
        )
        object_name = ".".join(
            str(part) for part in (table_name, column_name) if part is not None
        )
        return f"{qualifier} {object_name}".strip()
    item = difference[1] if len(difference) > 1 else None
    table = getattr(getattr(item, "table", None), "name", None)
    name = getattr(item, "name", None)
    object_name = ".".join(part for part in (table, name) if isinstance(part, str)) or (
        str(item) if item is not None else ""
    )
    return f"{qualifier} {object_name}".strip()


def _missing_check_constraints(connection: Connection) -> list[str]:
    """Report declared CHECK constraints the live catalog does not enforce.

    Autogenerate does not compare CHECK constraints at all, so this covers the
    one constraint kind a metadata comparison would otherwise miss.  Only
    declared names are compared; the expression text is the model's business.
    """

    from sqlalchemy import inspect

    from .models import Base

    inspector = inspect(connection)
    live_tables = set(inspector.get_table_names())
    missing: list[str] = []
    for table_name, table in sorted(Base.metadata.tables.items()):
        declared = {
            constraint.name
            for constraint in table.constraints
            if type(constraint).__name__ == "CheckConstraint"
            and isinstance(constraint.name, str)
        }
        if not declared or table_name not in live_tables:
            continue
        reflected = {
            constraint["name"]
            for constraint in inspector.get_check_constraints(table_name)
            if isinstance(constraint.get("name"), str)
        }
        missing.extend(
            f"missing check constraint {table_name}.{name}"
            for name in sorted(declared - reflected)
        )
    return missing


def verify_schema_is_current(connection: Connection) -> None:
    """Fail closed unless the live schema is exactly the current model schema.

    ``alembic_version`` reports the revision that was applied, never the shape
    that exists now.  This comparison is what turns a stale column or constraint
    left behind by an earlier baseline into an immediate, actionable startup
    refusal instead of a mysterious failure deep inside an operation.

    CHECK constraint *expressions* are not compared (declared names only),
    because autogenerate does not diff CHECK constraints.
    """

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from .models import Base

    differences = list(
        compare_metadata(MigrationContext.configure(connection), Base.metadata)
    )
    unexpected = [
        difference
        for difference in differences
        if _schema_difference_key(difference) not in _TOLERATED_SCHEMA_DIFFERENCES
    ]
    labels = sorted(
        {_schema_difference_label(difference) for difference in unexpected}
        | set(_missing_check_constraints(connection))
    )
    if not labels:
        return
    listed = ", ".join(labels[:_SCHEMA_DIFFERENCE_LIMIT])
    if len(labels) > _SCHEMA_DIFFERENCE_LIMIT:
        listed += f", and {len(labels) - _SCHEMA_DIFFERENCE_LIMIT} more"
    raise RuntimeError(
        "The live Controller database schema does not match the current "
        "Controller model metadata, so startup is refused rather than serving "
        "an operation against a stale schema. No automatic repair is performed. "
        f"Reconcile these objects, then restart the Controller: {listed}."
    )


def initialize_database(
    database_url: str,
    *,
    config_path: Path = _ALEMBIC_CONFIG,
) -> str:
    """Serialize schema migration and authority-head creation for API startup."""
    from .database_authority import DatabaseAuthorityService

    def initialize_once() -> str:
        engine = build_engine(database_url)
        try:
            if engine.dialect.name != "postgresql":
                raise RuntimeError(
                    "control database initialization requires PostgreSQL"
                )
            with engine.connect() as lock_connection:
                lock_connection.execute(
                    text("SELECT pg_advisory_lock(:key)"),
                    {"key": _STARTUP_ADVISORY_LOCK},
                )
                lock_connection.commit()
                try:
                    upgrade_schema(database_url, config_path=config_path)
                    with engine.connect() as schema_connection:
                        verify_schema_is_current(schema_connection)
                    authority = DatabaseAuthorityService(session_factory(engine))
                    return authority.ensure_initialized(acquire_advisory_lock=False)
                finally:
                    if lock_connection.in_transaction():
                        lock_connection.rollback()
                    lock_connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"),
                        {"key": _STARTUP_ADVISORY_LOCK},
                    )
                    lock_connection.commit()
        finally:
            engine.dispose()

    return run_with_database_startup_retry(initialize_once, label="PostgreSQL")
