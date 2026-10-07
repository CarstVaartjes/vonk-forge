"""Database engine, startup retry, and session construction."""

import copy
import logging
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import (
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
    TimeoutError,
)
from sqlalchemy.orm import Session, sessionmaker

from .settings import DATABASE_WAIT_BUDGETS

_STARTUP_ADVISORY_LOCK = 8_241_779_103
_MODULE_ALEMBIC_CONFIG = Path(__file__).resolve().parent / "alembic.ini"
_SOURCE_ALEMBIC_CONFIG = Path(__file__).resolve().parents[2] / "alembic.ini"
_ALEMBIC_CONFIG = (
    _MODULE_ALEMBIC_CONFIG
    if _MODULE_ALEMBIC_CONFIG.is_file()
    else _SOURCE_ALEMBIC_CONFIG
)
_DATABASE_STARTUP_TIMEOUT_SECONDS = 120.0
_DATABASE_RETRYABLE_ERRORS = (InterfaceError, OperationalError, TimeoutError)
_LOGGER = logging.getLogger(__name__)


def build_engine(database_url: str, *, component: str = "control") -> Engine:
    """Build the one engine every component shares.

    The fixed wait budgets bound every wait; PostgreSQL receives all four
    server-side timeouts per connection, and its pool is explicitly bounded so
    a saturated pool fails within the pool timeout instead of queueing a
    connection forever. SQLite accepts neither the server options nor an
    explicit pool size, so it keeps the default pool.

    ``component`` becomes the connections' ``application_name`` (``vonk:<name>``),
    so ``pg_stat_activity`` names the Controller process behind any session; an
    admission transaction narrows it to the kind of work it is doing.
    """

    budgets = DATABASE_WAIT_BUDGETS
    # Every JSON column write is checked against its contract (stored_columns);
    # a document no contract describes is reported once, never refused.
    from .stored_json import install_write_guard

    install_write_guard()
    if "postgres" in database_url:
        # transaction_timeout exists only on PostgreSQL 17+; the deployed
        # Compose service pins postgres:18.6@sha256:5a5a84b19854a9ffaa54082c166ff4ec27473a361e496e5ea167f298f2da9722, so the server accepts it.
        connect_args = {
            "options": " ".join(
                (
                    f"-c application_name=vonk:{component}",
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
    """Apply the baseline revision, then let startup reconcile live metadata."""
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
# current metadata is the only check that can see it, so startup reconciles it.


# Reviewed spurious reports, keyed by ``(operation, object name)``.  The
# declared ``UniqueConstraint`` on ``model_cache_set_artifacts`` covers that
# table's two primary-key columns, so PostgreSQL realises the same uniqueness as
# the primary key under that name.  Uniqueness is enforced; only the
# constraint-kind comparison disagrees, and it disagrees on a freshly created
# schema, so it is not drift.  ``test_migrations`` pins the same report.
_TOLERATED_SCHEMA_DIFFERENCES = frozenset(
    {("add_constraint", "uq_model_cache_set_artifact_key")}
)
_REPLACED_CONSTRAINT_NAMES = frozenset({"uq_control_process_heartbeats_kind"})

# A table the model retired is kept with its rows (schema reconciliation never
# drops data), but its foreign keys must not outlive it: a ``RESTRICT`` key
# would refuse the deletion of the revision or build a retained row names. Every
# key on a retired table is dropped, whatever it was named; nothing reads or
# writes the table any more.
_RETIRED_TABLES = frozenset({"runtime_image_authorizations"})


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


def _flatten_schema_differences(differences: Any) -> list[tuple[Any, ...]]:
    """Flatten Alembic's list-wrapped comparator results to individual diffs."""
    if not isinstance(differences, (list, tuple)):
        return []
    flattened: list[tuple[Any, ...]] = []
    for difference in differences:
        if isinstance(difference, list):
            flattened.extend(_flatten_schema_differences(difference))
        elif (
            isinstance(difference, tuple)
            and difference
            and isinstance(difference[0], str)
        ):
            flattened.append(difference)
    return flattened


def _check_constraint_differences(connection: Connection) -> list[str]:
    """Compare checks using PostgreSQL's own canonical expression rendering.

    Alembic omits CHECK expressions. Parse the declared checks on empty,
    connection-local temporary tables so casts, IN/ANY and parentheses receive
    the same normalization as the live catalog. PostgreSQL query planning then
    folds equivalent literal-array casts (which vary across server versions).
    No application rows are read. No durable schema is changed.
    """
    from sqlalchemy import CheckConstraint, Column, MetaData, Table, inspect
    from sqlalchemy.schema import CreateTable, DropTable

    from .models import Base

    inspector = inspect(connection)
    live_tables = set(inspector.get_table_names())
    differences: list[str] = []
    for table_name, table in sorted(Base.metadata.tables.items()):
        declared = {
            constraint.name: constraint
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
            and isinstance(constraint.name, str)
        }
        if not declared or table_name not in live_tables:
            continue
        reflected = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints(table_name)
            if isinstance(constraint.get("name"), str)
        }
        differences.extend(
            f"missing check constraint {table_name}.{name}"
            for name in sorted(declared.keys() - reflected.keys())
        )
        if connection.dialect.name != "postgresql":
            continue
        temporary = Table(
            "vonk_schema_check_" + uuid.uuid4().hex,
            MetaData(),
            *(Column(column.name, column.type) for column in table.columns),
            *(
                CheckConstraint(
                    str(
                        check.sqltext.compile(
                            dialect=connection.dialect,
                            compile_kwargs={"literal_binds": True},
                        )
                    ),
                    name=name,
                )
                for name, check in declared.items()
            ),
            prefixes=["TEMPORARY"],
        )
        connection.execute(CreateTable(temporary))
        try:
            expected = {
                check["name"]: check["sqltext"]
                for check in inspect(connection).get_check_constraints(temporary.name)
            }

            def normalized(expression: str, table_name: str = temporary.name) -> object:
                quoted_table = connection.dialect.identifier_preparer.quote(table_name)
                plan = connection.exec_driver_sql(
                    f"EXPLAIN (VERBOSE, FORMAT JSON) SELECT ({expression}) FROM {quoted_table}"
                ).scalar_one()
                return plan[0]["Plan"]["Output"]

            def same(expected_sql: str, reflected_sql: str) -> bool:
                if expected_sql == reflected_sql:
                    return True
                # A live check may reference a column the model no longer has;
                # it cannot match the declared one, so it gets replaced.
                try:
                    with connection.begin_nested():
                        return normalized(expected_sql) == normalized(reflected_sql)
                except SQLAlchemyError:
                    return False

            differences.extend(
                f"changed check constraint {table_name}.{name}"
                for name in sorted(declared.keys() & reflected.keys())
                if not same(expected[name], reflected[name])
            )
        finally:
            connection.execute(DropTable(temporary))
    return differences


def _column_default_sql(column: object, connection: Connection) -> str | None:
    """Return a safe SQL expression for a declared scalar model default."""
    from sqlalchemy import DefaultClause

    server_default = getattr(column, "server_default", None)
    if isinstance(server_default, DefaultClause):
        argument = server_default.arg
        compile_default = getattr(argument, "compile", None)
        if compile_default is None:
            return str(argument)
        return str(compile_default(dialect=connection.dialect))
    default = getattr(column, "default", None)
    value = getattr(default, "arg", None)
    if value is None or callable(value):
        return None
    literal = getattr(getattr(column, "type", None), "literal_processor", None)
    processor = literal(connection.dialect) if literal else None
    if processor is None:
        return None
    return processor(value)


def _cosmetic_type_difference(existing_type: object, model_type: object) -> bool:
    """Recognize dialect spellings for the same binary/date-time storage type."""
    from sqlalchemy import DateTime, LargeBinary

    existing_name = type(existing_type).__name__.upper()
    if isinstance(model_type, DateTime) and existing_name in {
        "DATETIME",
        "TIMESTAMP",
    }:
        return True
    return isinstance(model_type, LargeBinary) and existing_name in {"BLOB", "BYTEA"}


def _is_generated_sequence_default(default: object, reflected_column: object) -> bool:
    """Never remove PostgreSQL serial/identity ownership during reconciliation."""
    if getattr(reflected_column, "get", lambda *_: None)("identity"):
        return True
    rendered = str(default or "").lower()
    return "nextval(" in rendered


def _constraint_kind(constraint: object) -> str | None:
    from sqlalchemy import (
        CheckConstraint,
        ForeignKeyConstraint,
        PrimaryKeyConstraint,
        UniqueConstraint,
    )

    if isinstance(constraint, CheckConstraint):
        return "check"
    if isinstance(constraint, ForeignKeyConstraint):
        return "foreignkey"
    if isinstance(constraint, PrimaryKeyConstraint):
        return "primary"
    if isinstance(constraint, UniqueConstraint):
        return "unique"
    return None


def _quoted(connection: Connection, *names: str) -> str:
    preparer = connection.dialect.identifier_preparer
    return ".".join(preparer.quote(name) for name in names)


def _repair_check_constraints(connection: Connection) -> None:
    """Converge named CHECKs; invalid historical rows do not block startup."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import CheckConstraint, inspect

    from .models import Base

    ops = Operations(MigrationContext.configure(connection))
    inspector = inspect(connection)
    live_tables = set(inspector.get_table_names())
    differences = set(_check_constraint_differences(connection))
    for table_name, table in sorted(Base.metadata.tables.items()):
        if table_name not in live_tables:
            continue
        reflected = {
            item["name"]: item
            for item in inspector.get_check_constraints(table_name)
            if item.get("name")
        }
        for constraint in table.constraints:
            name = constraint.name
            if not isinstance(constraint, CheckConstraint) or not isinstance(name, str):
                continue
            existing = reflected.get(name)
            mismatch = (
                existing is not None
                and f"changed check constraint {table_name}.{name}" in differences
            )
            if existing is not None and not mismatch:
                continue
            expression = str(
                constraint.sqltext.compile(
                    dialect=connection.dialect,
                    compile_kwargs={"literal_binds": True},
                )
            )
            try:
                with connection.begin_nested():
                    if mismatch:
                        ops.drop_constraint(name, table_name, type_="check")
                    ops.create_check_constraint(name, table_name, expression)
                _LOGGER.info(
                    "%s CHECK constraint %s.%s",
                    "Reconciled" if mismatch else "Created",
                    table_name,
                    name,
                )
            except SQLAlchemyError as error:
                if connection.dialect.name != "postgresql":
                    _LOGGER.warning(
                        "Could not create CHECK %s.%s: %s",
                        table_name,
                        name,
                        error,
                    )
                    continue
                quoted_table = _quoted(connection, table_name)
                quoted_constraint = connection.dialect.identifier_preparer.quote(name)
                try:
                    with connection.begin_nested():
                        connection.exec_driver_sql(
                            f"ALTER TABLE {quoted_table} ADD CONSTRAINT {quoted_constraint} "
                            f"CHECK ({expression}) NOT VALID"
                        )
                    _LOGGER.warning(
                        "Created CHECK %s.%s NOT VALID because existing rows violate it: %s",
                        table_name,
                        name,
                        error,
                    )
                except SQLAlchemyError as deferred_error:
                    _LOGGER.warning(
                        "Could not create CHECK %s.%s: %s",
                        table_name,
                        name,
                        deferred_error,
                    )


def _release_retired_table_foreign_keys(connection: Connection, ops: Any) -> None:
    """Drop the foreign keys of retired tables so their old rows never block."""

    if connection.dialect.name != "postgresql":
        return
    from sqlalchemy import inspect

    inspector = inspect(connection)
    live_tables = set(inspector.get_table_names())
    for table_name in sorted(_RETIRED_TABLES & live_tables):
        for key in inspector.get_foreign_keys(table_name):
            name = key["name"]
            if not name:
                continue
            try:
                with connection.begin_nested():
                    ops.drop_constraint(name, table_name, type_="foreignkey")
                _LOGGER.info("Dropped retired foreign key %s.%s", table_name, name)
            except SQLAlchemyError as error:
                _LOGGER.warning(
                    "Could not drop retired foreign key %s.%s: %s",
                    table_name,
                    name,
                    error,
                )


def reconcile_schema(connection: Connection) -> None:
    """Apply safe metadata differences inside the caller's startup transaction."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    from .models import Base

    ops = Operations(MigrationContext.configure(connection))
    differences = [
        difference
        for difference in _flatten_schema_differences(
            compare_metadata(
                MigrationContext.configure(
                    connection, opts={"compare_server_default": True}
                ),
                Base.metadata,
            )
        )
        if _schema_difference_key(difference) not in _TOLERATED_SCHEMA_DIFFERENCES
    ]
    inspector = inspect(connection)
    tables = set(inspector.get_table_names()) - {"alembic_version"}
    metadata_tables = set(Base.metadata.tables)
    column_key = lambda difference: (
        str(difference[2]),
        str(getattr(difference[3], "name", difference[3])),
    )
    reported_type_changes = {
        column_key(difference)
        for difference in differences
        if difference[0] == "modify_type"
    }
    reported_default_changes = {
        column_key(difference)
        for difference in differences
        if difference[0] == "modify_default"
    }
    reported_nullable_changes = {
        column_key(difference)
        for difference in differences
        if difference[0] == "modify_nullable"
    }

    # Create absent tables as a group so foreign-key dependencies are present.
    for table in Base.metadata.sorted_tables:
        if table.name in tables:
            continue
        table.create(connection, checkfirst=True)
        _LOGGER.info("Created missing table %s", table.name)

    # Add missing columns. A scalar default is used only to backfill an
    # otherwise-required column; callable defaults are not safe bulk values.
    inspector = inspect(connection)
    for difference in differences:
        if difference[0] != "add_column":
            continue
        table_name, model_column = difference[2], difference[3]
        column = copy.copy(model_column)
        default_sql = _column_default_sql(model_column, connection)
        if not column.nullable and column.server_default is None:
            if default_sql is None:
                column.nullable = True
                _LOGGER.warning(
                    "Added required column %s.%s as nullable because its model has no safe scalar default",
                    table_name,
                    column.name,
                )
            else:
                from sqlalchemy import DefaultClause

                column.server_default = DefaultClause(text(default_sql))
        try:
            with connection.begin_nested():
                ops.add_column(table_name, column)
        except SQLAlchemyError as error:
            _LOGGER.warning(
                "Skipped missing column %s.%s: %s", table_name, column.name, error
            )
            continue
        _LOGGER.info("Added column %s.%s", table_name, model_column.name)
        if (
            not model_column.nullable
            and model_column.server_default is None
            and default_sql
        ):
            try:
                with connection.begin_nested():
                    ops.alter_column(
                        table_name,
                        model_column.name,
                        server_default=None,
                        existing_type=model_column.type,
                    )
            except SQLAlchemyError as error:
                _LOGGER.warning(
                    "Kept temporary backfill default on %s.%s: %s",
                    table_name,
                    model_column.name,
                    error,
                )

    # Change types/defaults/nullability before constraint installation. A
    # PostgreSQL cast failure is scoped to that column and does not poison startup.
    for table_name, table in Base.metadata.tables.items():
        if table_name not in tables:
            continue
        live_columns = {
            column["name"]: column for column in inspector.get_columns(table_name)
        }
        primary_key_columns = set(
            inspector.get_pk_constraint(table_name).get("constrained_columns") or ()
        )
        for column in table.columns:
            old = live_columns.get(column.name)
            if old is None:
                continue
            try:
                if (
                    table_name,
                    column.name,
                ) in reported_type_changes and not _cosmetic_type_difference(
                    old["type"], column.type
                ):
                    if (
                        column.name in primary_key_columns
                        or column.primary_key
                        or old.get("identity")
                        or getattr(column, "identity", None) is not None
                    ):
                        _LOGGER.warning(
                            "Skipped type change for primary-key or identity column %s.%s",
                            table_name,
                            column.name,
                        )
                        continue
                    if connection.dialect.name != "postgresql":
                        _LOGGER.warning(
                            "Skipped unverified type change for %s.%s on %s",
                            table_name,
                            column.name,
                            connection.dialect.name,
                        )
                        continue
                    quoted = connection.dialect.identifier_preparer.quote(column.name)
                    quoted_table = _quoted(connection, table_name)
                    target_type = column.type.compile(dialect=connection.dialect)
                    using = f"{quoted}::{target_type}"
                    with connection.begin_nested():
                        lossy_value = connection.exec_driver_sql(
                            f"SELECT 1 FROM {quoted_table} WHERE {quoted} IS NOT NULL "
                            f"AND NOT ({quoted}::{target_type} = {quoted}) LIMIT 1"
                        ).first()
                        if lossy_value is not None:
                            _LOGGER.warning(
                                "Skipped lossy type change for %s.%s; existing values do not compare equal after conversion",
                                table_name,
                                column.name,
                            )
                        else:
                            ops.alter_column(
                                table_name,
                                column.name,
                                type_=column.type,
                                existing_type=old["type"],
                                postgresql_using=using,
                            )
                    if lossy_value is None:
                        _LOGGER.info("Changed type of %s.%s", table_name, column.name)
            except SQLAlchemyError as error:
                _LOGGER.warning(
                    "Skipped uncastable type change for %s.%s: %s",
                    table_name,
                    column.name,
                    error,
                )
            if (
                (table_name, column.name) in reported_nullable_changes
                and old.get("nullable")
                and not column.nullable
            ):
                default_sql = _column_default_sql(column, connection)
                try:
                    with connection.begin_nested():
                        if default_sql:
                            quoted_table = _quoted(connection, table_name)
                            quoted_column = (
                                connection.dialect.identifier_preparer.quote(
                                    column.name
                                )
                            )
                            connection.exec_driver_sql(
                                f"UPDATE {quoted_table} SET {quoted_column}={default_sql} "
                                f"WHERE {quoted_column} IS NULL"
                            )
                        ops.alter_column(
                            table_name,
                            column.name,
                            nullable=False,
                            existing_type=column.type,
                        )
                    _LOGGER.info("Made column %s.%s NOT NULL", table_name, column.name)
                except SQLAlchemyError as error:
                    _LOGGER.warning(
                        "Skipped NOT NULL change for %s.%s: %s",
                        table_name,
                        column.name,
                        error,
                    )
            elif (
                (table_name, column.name) in reported_nullable_changes
                and not old.get("nullable")
                and column.nullable
            ):
                try:
                    with connection.begin_nested():
                        ops.alter_column(
                            table_name,
                            column.name,
                            nullable=True,
                            existing_type=column.type,
                        )
                    _LOGGER.info("Made column %s.%s nullable", table_name, column.name)
                except SQLAlchemyError as error:
                    _LOGGER.warning(
                        "Skipped nullable change for %s.%s: %s",
                        table_name,
                        column.name,
                        error,
                    )
            existing_default = old.get("default")
            if (
                connection.dialect.name == "postgresql"
                and (table_name, column.name) in reported_default_changes
                and not _is_generated_sequence_default(existing_default, old)
            ):
                target_sql = _column_default_sql(column, connection)
                try:
                    with connection.begin_nested():
                        ops.alter_column(
                            table_name,
                            column.name,
                            server_default=target_sql,
                            existing_type=column.type,
                        )
                    _LOGGER.info(
                        "Changed server default for %s.%s", table_name, column.name
                    )
                except SQLAlchemyError as error:
                    _LOGGER.warning(
                        "Skipped server default change for %s.%s: %s",
                        table_name,
                        column.name,
                        error,
                    )

    # Unknown columns on model-owned tables can prevent new rows from being
    # inserted when they are NOT NULL without a default. Keep their data and
    # definition, but remove that insertion barrier. Tables outside our models
    # remain completely untouched.
    inspector = inspect(connection)
    for table_name in sorted(tables & metadata_tables):
        model_columns = set(Base.metadata.tables[table_name].columns.keys())
        for old in inspector.get_columns(table_name):
            if (
                old["name"] in model_columns
                or old.get("nullable", True)
                or old.get("default") is not None
            ):
                continue
            try:
                with connection.begin_nested():
                    ops.alter_column(
                        table_name,
                        old["name"],
                        nullable=True,
                        existing_type=old["type"],
                    )
            except SQLAlchemyError as error:
                _LOGGER.warning(
                    "Could not make extra column %s.%s nullable: %s",
                    table_name,
                    old["name"],
                    error,
                )
                continue
            _LOGGER.warning(
                "Made extra column %s.%s nullable so inserts can continue; existing data was retained",
                table_name,
                old["name"],
            )
    # Install indexes and unique constraints before dropping old forms. Savepoints
    # let a data conflict skip just the new uniqueness rule.
    failed_index_tables: set[str] = set()
    failed_constraint_tables: set[str] = set()
    for difference in differences:
        if difference[0] == "add_index":
            index = difference[1]
            try:
                with connection.begin_nested():
                    index.create(connection, checkfirst=True)
                _LOGGER.info("Created index %s", index.name)
            except SQLAlchemyError as error:
                failed_index_tables.add(index.table.name)
                _LOGGER.warning("Skipped index %s: %s", index.name, error)
        elif difference[0] == "add_constraint":
            constraint = difference[1]
            if getattr(constraint, "name", None) == "uq_model_cache_set_artifact_key":
                continue
            kind = _constraint_kind(constraint)
            if kind == "check":
                continue
            try:
                from alembic.operations.ops import AddConstraintOp

                with connection.begin_nested():
                    ops.invoke(AddConstraintOp.from_constraint(constraint))
                _LOGGER.info(
                    "Created %s constraint %s", kind or "database", constraint.name
                )
            except SQLAlchemyError as error:
                failed_constraint_tables.add(constraint.table.name)
                _LOGGER.warning(
                    "Skipped constraint %s due to existing data or unsupported change: %s",
                    constraint.name,
                    error,
                )

    _repair_check_constraints(connection)

    # Drop obsolete constraints/indexes before their columns.
    added_constraints = {
        (
            difference[1].table.name,
            difference[1].name,
            _constraint_kind(difference[1]),
            frozenset(column.name for column in difference[1].columns),
        )
        for difference in differences
        if difference[0] == "add_constraint"
    }
    added_indexes = {
        (difference[1].table.name, difference[1].name)
        for difference in differences
        if difference[0] == "add_index"
    }
    for difference in differences:
        operation = difference[0]
        if operation == "remove_constraint":
            constraint = difference[1]
            name = getattr(constraint, "name", None)
            kind = _constraint_kind(constraint)
            table_name = constraint.table.name
            if (
                name
                and kind
                and table_name in metadata_tables
                and (
                    name in _REPLACED_CONSTRAINT_NAMES
                    or any(
                        added_table == table_name
                        and (
                            added_name == name
                            or (
                                kind == "unique"
                                and added_kind == "unique"
                                and added_columns
                                == frozenset(
                                    column.name for column in constraint.columns
                                )
                            )
                        )
                        for (
                            added_table,
                            added_name,
                            added_kind,
                            added_columns,
                        ) in added_constraints
                    )
                )
            ):
                if constraint.table.name in failed_constraint_tables:
                    _LOGGER.warning(
                        "Retained obsolete constraint %s.%s because its replacement could not be installed",
                        constraint.table.name,
                        name,
                    )
                    continue
                try:
                    with connection.begin_nested():
                        ops.drop_constraint(name, constraint.table.name, type_=kind)
                    _LOGGER.info(
                        "Dropped obsolete constraint %s.%s", constraint.table.name, name
                    )
                except SQLAlchemyError as error:
                    _LOGGER.warning(
                        "Could not drop obsolete constraint %s: %s", name, error
                    )
        elif operation == "remove_index":
            index = difference[1]
            if (
                index.table.name not in metadata_tables
                or (
                    index.table.name,
                    index.name,
                )
                not in added_indexes
            ):
                continue
            if index.table.name in failed_index_tables:
                _LOGGER.warning(
                    "Retained obsolete index %s because its replacement could not be installed",
                    index.name,
                )
                continue
            try:
                with connection.begin_nested():
                    index.drop(connection, checkfirst=True)
                _LOGGER.info("Dropped obsolete index %s", index.name)
            except SQLAlchemyError as error:
                _LOGGER.warning(
                    "Could not drop obsolete index %s: %s", index.name, error
                )

    _release_retired_table_foreign_keys(connection, ops)

    remaining = _check_constraint_differences(connection)
    if remaining:
        _LOGGER.warning(
            "Schema reconciliation left deferred CHECK constraints: %s",
            ", ".join(remaining),
        )


def verify_schema_is_current(connection: Connection) -> None:
    """Reconcile the live catalog to current metadata within this transaction."""
    reconcile_schema(connection)


def adopt_legacy_rows(connection: Connection) -> None:
    """Move rows written before a model change onto their current encoding.

    Idempotent and bounded; it runs inside the startup advisory lock, after the
    schema is current.  Rows it misses are adopted lazily by their kind's
    lifecycle adapter, so a gap here heals instead of stranding a row.
    """
    from .lifecycle.agent_operation import adopt_legacy_orders

    adopted = adopt_legacy_orders(connection)
    if adopted:
        _LOGGER.info("Adopted %d Spark orders onto the lifecycle schedule", adopted)
    from .artifact_job_states import adopt_legacy_artifact_jobs

    adopted = adopt_legacy_artifact_jobs(connection)
    if adopted:
        _LOGGER.info("Adopted %d artifact jobs onto the core state vocabulary", adopted)
    from .lifecycle.model_cache import adopt_legacy_operations

    adopted = adopt_legacy_operations(connection)
    if adopted:
        _LOGGER.info("Adopted %d cache operations onto the lifecycle schedule", adopted)
    from .legacy_states import adopt_legacy_states

    adopted = adopt_legacy_states(connection)
    if adopted:
        _LOGGER.info("Rewrote %d lifecycle rows onto the core state words", adopted)


def initialize_database(
    database_url: str,
    *,
    config_path: Path = _ALEMBIC_CONFIG,
) -> None:
    """Serialize schema migration and reconciliation for API startup."""

    def initialize_once() -> None:
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
                    try:
                        with engine.begin() as schema_connection:
                            verify_schema_is_current(schema_connection)
                            # Adoption is derived bookkeeping, not schema authority.
                            # A savepoint keeps a damaged historical row from undoing
                            # current schema reconciliation; readers heal rows lazily.
                            try:
                                with schema_connection.begin_nested():
                                    adopt_legacy_rows(schema_connection)
                            except SQLAlchemyError:
                                _LOGGER.exception(
                                    "Lifecycle bookkeeping adoption deferred; "
                                    "current schema remains available"
                                )
                    except SQLAlchemyError as error:
                        raise RuntimeError(
                            "Controller startup schema reconciliation failed and the "
                            "transaction was rolled back. This error class is not "
                            "retryable, so startup aborts; schema failure: "
                            f"{error}"
                        ) from error
                    # Retained profile journals need exact SQL child proof before
                    # the sole current reader can resume them. One short bounded
                    # page runs under this startup owner; normal worker passes
                    # continue remaining or temporarily locked rows automatically.
                    from .fleet_profile_adapter_conversion import (
                        convert_due_retained_applications,
                    )

                    converted = convert_due_retained_applications(
                        sessionmaker(engine, expire_on_commit=False), datetime.now(UTC)
                    )
                    if converted:
                        _LOGGER.info(
                            "Converted %d retained profile adapter journals", converted
                        )
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

    run_with_database_startup_retry(initialize_once, label="PostgreSQL")
