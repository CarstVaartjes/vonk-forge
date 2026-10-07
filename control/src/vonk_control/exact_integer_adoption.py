"""Transactional, owning-column adoption of exact decimal integer storage."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    CheckConstraint,
    Column,
    MetaData,
    Table,
    Text,
    inspect,
    literal_column,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from .exact_integer_storage import DecimalIntegerToken
from .settings import DATABASE_WAIT_BUDGETS

_COLUMNS = {
    "catalog_document_revisions": ("download_bytes", "installed_bytes"),
    "model_cache_sets": ("expected_bytes", "verified_bytes"),
}
_OWNED_CHECKS = {
    "catalog_document_revisions": (
        "ck_catalog_document_revision_download_bytes",
        "ck_catalog_document_revision_installed_bytes",
    ),
    "model_cache_sets": ("ck_model_cache_sets_sizes",),
}


def _validate_rows(
    connection: Connection, table_name: str, columns: tuple[str, ...]
) -> None:
    from .models import Base

    table = Base.metadata.tables[table_name]
    predicates = []
    for name in columns:
        token = DecimalIntegerToken(literal_column(name))
        expression = str(token.compile(dialect=connection.dialect))
        if table.c[name].nullable:
            predicates.append(f"({name} IS NOT NULL AND NOT ({expression}))")
        else:
            predicates.append(f"({name} IS NULL OR NOT ({expression}))")
    if table_name == "model_cache_sets":
        # Before any mutation, the old owning values must also obey their
        # cross-field bound. This validates exact token magnitude, not affinity.
        from .exact_integer_storage import DecimalIntegerAtMost

        order = DecimalIntegerAtMost(
            literal_column("verified_bytes"), literal_column("expected_bytes")
        )
        predicates.append(f"NOT ({order.compile(dialect=connection.dialect)})")
    invalid = connection.exec_driver_sql(
        f"SELECT 1 FROM {table_name} WHERE {' OR '.join(predicates)} LIMIT 1"
    ).first()
    if invalid is not None:
        raise ValueError(f"exact integer adoption refused invalid rows in {table_name}")


def _postgres_owned_checks_current(
    connection: Connection, table_name: str, names: tuple[str, ...]
) -> bool:
    """Compare only owning CHECKs using the server's exact native rendering."""
    from sqlalchemy.schema import CreateTable, DropTable

    from .models import Base

    declared = Base.metadata.tables[table_name]
    temporary = Table(
        "vonk_exact_integer_check_" + uuid.uuid4().hex,
        MetaData(),
        *(Column(name, Text(), nullable=declared.c[name].nullable) for name in names),
        *(
            CheckConstraint(constraint.sqltext, name=constraint.name)
            for constraint in declared.constraints
            if isinstance(constraint, CheckConstraint)
            and constraint.name in _OWNED_CHECKS[table_name]
        ),
        prefixes=["TEMPORARY"],
    )
    connection.execute(CreateTable(temporary))
    try:
        expected = {
            check["name"]: check["sqltext"]
            for check in inspect(connection).get_check_constraints(temporary.name)
        }
        actual = {
            check["name"]: check["sqltext"]
            for check in inspect(connection).get_check_constraints(table_name)
        }
        validated = {
            name: value
            for name, value in connection.exec_driver_sql(
                "SELECT conname,convalidated FROM pg_constraint "
                "WHERE conrelid=to_regclass(%s) AND contype='c'",
                (table_name,),
            )
        }
        return all(
            validated.get(name) is True and actual.get(name) == expression
            for name, expression in expected.items()
        )
    finally:
        connection.execute(DropTable(temporary))


def _pending_adoption(connection: Connection) -> dict[str, tuple[str, ...]]:
    from .models import Base

    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    pending = {}
    for table_name, names in _COLUMNS.items():
        if table_name not in tables:
            continue
        if connection.dialect.name == "postgresql":
            # Native constraint rendering can acquire a relation read lock.
            # Fence observation NOWAIT before reflection; ordinary writers
            # remain compatible. Actual adoption upgrades separately NOWAIT.
            connection.exec_driver_sql(
                f"LOCK TABLE {table_name} IN ACCESS SHARE MODE NOWAIT"
            )
        columns = {
            column["name"]: column for column in inspector.get_columns(table_name)
        }
        if not all(name in columns for name in names):
            continue
        checks = {
            check["name"]: check["sqltext"]
            for check in inspector.get_check_constraints(table_name)
        }
        needs_check = any(name not in checks for name in _OWNED_CHECKS[table_name])
        if connection.dialect.name == "sqlite":
            # SQLite preserves the declared expression spelling. Compare only
            # our owned canonical expressions, never normalize unknown checks.
            normalize = lambda value: str(value).strip()
            for constraint in Base.metadata.tables[table_name].constraints:
                if (
                    isinstance(constraint, CheckConstraint)
                    and isinstance(constraint.name, str)
                    and constraint.name in _OWNED_CHECKS[table_name]
                ):
                    expected = constraint.sqltext.compile(dialect=connection.dialect)
                    needs_check |= normalize(checks.get(constraint.name)) != normalize(
                        expected
                    )
        needs_type = any(not isinstance(columns[name]["type"], Text) for name in names)
        if (
            connection.dialect.name == "postgresql"
            and not needs_type
            and not needs_check
        ):
            needs_check = not _postgres_owned_checks_current(
                connection, table_name, names
            )
        if needs_check or needs_type:
            pending[table_name] = names
    return pending


def _sqlite_extensions(
    connection: Connection, table_name: str, names: tuple[str, ...]
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    """Preserve native expression-index DDL; defer unproved numeric semantics."""
    table_sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).scalar_one()
    if not isinstance(table_sql, str):
        raise TypeError(f"unreadable SQLite table definition: {table_name}")
    # SQLAlchemy preserves reflected CHECK expressions, but loses column
    # COLLATE. Remove known reflected expressions before checking the remaining
    # declaration; unsupported column collation must not silently disappear.
    for check in inspect(connection).get_check_constraints(table_name):
        expression = check["sqltext"]
        if isinstance(expression, str):
            table_sql = table_sql.replace(expression, "")
    if re.search(r"\bCOLLATE\b", table_sql, flags=re.IGNORECASE):
        raise ValueError(
            f"exact integer adoption deferred: {table_name} has column collation "
            "not preserved by reflection; review its native DDL before retrying"
        )
    if re.search(r"\bAUTOINCREMENT\b", table_sql, flags=re.IGNORECASE):
        raise ValueError(
            f"exact integer adoption deferred: {table_name} has AUTOINCREMENT "
            "high-water state not preserved by reflection; review its native "
            "DDL and sequence before retrying"
        )
    indexes = []
    triggers = []
    rows = connection.exec_driver_sql(
        "SELECT type,name,sql,tbl_name FROM sqlite_master "
        "WHERE (tbl_name=? AND type='index') OR type IN ('trigger','view') ORDER BY name",
        (table_name,),
    )
    for kind, identity, definition, owning_table in rows:
        if definition is None and kind == "index":
            continue  # Table-owned UNIQUE/PK autoindexes are copied as constraints.
        if not isinstance(definition, str) or not isinstance(identity, str):
            raise TypeError(f"unreadable SQLite schema extension: {table_name}")
        if (
            kind in {"view", "trigger"}
            and owning_table != table_name
            and re.search(
                rf"(?<![A-Za-z0-9_]){table_name}(?![A-Za-z0-9_])",
                definition,
                re.IGNORECASE,
            )
            is None
        ):
            continue
        # Wildcard reads can consume the changed columns without naming them.
        # Direct owner views are checked before any alteration; refusing an
        # unproved direct wildcard also protects views layered above it.
        wildcard_read = kind in {"view", "trigger"} and "*" in definition
        if wildcard_read or any(
            re.search(
                rf"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])", definition, re.IGNORECASE
            )
            for name in names
        ):
            raise ValueError(
                f"exact integer adoption deferred: {table_name}.{identity} "
                f"has an unreviewed numeric {kind}; review its decimal TEXT "
                "semantics before retrying schema adoption"
            )
        if kind == "index":
            indexes.append((identity, definition))
        elif kind == "trigger" and owning_table == table_name:
            triggers.append(definition)
    return tuple(indexes), tuple(triggers)


def _postgres_extensions(
    connection: Connection, table_name: str, names: tuple[str, ...]
) -> None:
    """Defer unproved numeric extensions before PostgreSQL changes a type.

    Native dependency records include expression/partial indexes and expanded
    view wildcards. Procedural trigger bodies have no reliable column dependency
    records, so unexpected user triggers require explicit semantic review.
    """
    columns = {
        column["name"]: column for column in inspect(connection).get_columns(table_name)
    }
    changed = tuple(
        name for name in names if not isinstance(columns[name]["type"], Text)
    )
    if not changed:
        return
    for name in changed:
        if columns[name]["default"] is not None:
            raise ValueError(
                f"exact integer adoption deferred: {table_name}.{name} has an "
                "unreviewed server default; review its decimal TEXT semantics "
                "before retrying schema adoption"
            )
    dependencies = connection.exec_driver_sql(
        "SELECT DISTINCT CASE WHEN d.classid='pg_rewrite'::regclass THEN 'view' "
        "ELSE 'index' END, c.relname "
        "FROM pg_depend d JOIN pg_attribute a "
        "ON a.attrelid=d.refobjid AND a.attnum=d.refobjsubid "
        "LEFT JOIN pg_rewrite r ON d.classid='pg_rewrite'::regclass AND r.oid=d.objid "
        "JOIN pg_class c ON c.oid=CASE WHEN d.classid='pg_rewrite'::regclass "
        "THEN r.ev_class ELSE d.objid END "
        "WHERE d.refclassid='pg_class'::regclass AND d.refobjid=to_regclass(%s) "
        "AND a.attname=ANY(%s) "
        "AND (d.classid='pg_rewrite'::regclass OR "
        "(d.classid='pg_class'::regclass AND c.relkind IN ('i','I'))) "
        "ORDER BY 1,2",
        (table_name, list(changed)),
    ).first()
    if dependencies is not None:
        kind, identity = dependencies
        raise ValueError(
            f"exact integer adoption deferred: {table_name}.{identity} has an "
            f"unreviewed numeric {kind}; review its decimal TEXT semantics "
            "before retrying schema adoption"
        )
    trigger = connection.exec_driver_sql(
        "SELECT tgname FROM pg_trigger WHERE tgrelid=to_regclass(%s) "
        "AND NOT tgisinternal ORDER BY tgname LIMIT 1",
        (table_name,),
    ).scalar()
    if trigger is not None:
        raise ValueError(
            f"exact integer adoption deferred: {table_name}.{trigger} has an "
            "unreviewed procedural trigger; review its decimal TEXT semantics "
            "before retrying schema adoption"
        )


def adopt_exact_integer_columns(connection: Connection) -> None:
    """Adopt only the four metadata projections, preserving unrelated schema.

    The caller owns the transaction. SQLite callers with foreign-key
    enforcement must use reconcile_exact_integer_schema before service use.
    """
    from .models import Base

    pending = _pending_adoption(connection)
    if not pending:
        return
    if connection.dialect.name not in {"postgresql", "sqlite"}:
        raise ValueError("exact integer adoption requires PostgreSQL or SQLite")
    if connection.dialect.name == "sqlite":
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one():
            raise ValueError(
                "SQLite exact integer adoption requires its isolated schema entry"
            )
        # sqlite3's legacy transaction mode does not BEGIN for DDL. Establish
        # a real transaction before the savepoint/rebuild so rollback is real.
        driver = connection.connection.driver_connection
        if not getattr(driver, "in_transaction", False):
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    with connection.begin_nested():
        # Validate the entire owning scope before changing either table. A
        # denied scan or malformed token establishes no migration authority.
        if connection.dialect.name == "postgresql":
            for table_name in sorted(pending):
                connection.exec_driver_sql(
                    f"LOCK TABLE {table_name} IN ACCESS EXCLUSIVE MODE NOWAIT"
                )
        extensions = {}
        for table_name, names in pending.items():
            if connection.dialect.name == "sqlite":
                extensions[table_name] = _sqlite_extensions(
                    connection, table_name, names
                )
            else:
                _postgres_extensions(connection, table_name, names)
            for check in inspect(connection).get_check_constraints(table_name):
                if check["name"] in _OWNED_CHECKS[table_name]:
                    continue
                expression = check["sqltext"]
                if not isinstance(expression, str):
                    raise TypeError(f"unreadable check constraint in {table_name}")
                if any(
                    re.search(
                        rf"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])",
                        expression,
                        flags=re.IGNORECASE,
                    )
                    for name in names
                ):
                    identity = check["name"] or (
                        "unnamed-"
                        + hashlib.sha256(expression.encode()).hexdigest()[:16]
                    )
                    raise ValueError(
                        f"exact integer adoption deferred: {table_name}.{identity} "
                        "has an unreviewed numeric check; review its decimal TEXT "
                        "semantics before retrying schema adoption"
                    )
            _validate_rows(connection, table_name, names)
        ops = Operations(MigrationContext.configure(connection))
        for table_name, names in pending.items():
            declared = Base.metadata.tables[table_name]
            if connection.dialect.name == "postgresql":
                checks = {
                    check["name"]
                    for check in inspect(connection).get_check_constraints(table_name)
                }
                for name in _OWNED_CHECKS[table_name]:
                    if isinstance(name, str) and name in checks:
                        ops.drop_constraint(name, table_name, type_="check")
                for name in names:
                    # Canonical decimal validation above proves the cast and
                    # its reverse preserve every existing integer exactly.
                    old_type = next(
                        column["type"]
                        for column in inspect(connection).get_columns(table_name)
                        if column["name"] == name
                    )
                    if isinstance(old_type, Text):
                        continue  # Its validated token is already the owner encoding.
                    original_type = old_type.compile(dialect=connection.dialect)
                    lossy = connection.exec_driver_sql(
                        f"SELECT 1 FROM {table_name} WHERE {name} IS NOT NULL "
                        f"AND ({name}::text)::{original_type} <> {name} LIMIT 1"
                    ).first()
                    if lossy is not None:
                        raise ValueError(
                            f"lossy exact integer adoption: {table_name}.{name}"
                        )
                    ops.alter_column(
                        table_name, name, type_=Text(), postgresql_using=f"{name}::text"
                    )
                for constraint in declared.constraints:
                    if (
                        isinstance(constraint, CheckConstraint)
                        and isinstance(constraint.name, str)
                        and constraint.name in _OWNED_CHECKS[table_name]
                    ):
                        ops.create_check_constraint(
                            constraint.name, table_name, constraint.sqltext
                        )
            else:
                reflected = Table(table_name, MetaData(), autoload_with=connection)
                for constraint in list(reflected.constraints):
                    if isinstance(constraint, CheckConstraint):
                        if constraint.name in _OWNED_CHECKS[table_name]:
                            reflected.constraints.remove(constraint)
                        elif constraint.name is None:
                            # Alembic omits unnamed CHECKs during rebuild;
                            # give the preserved expression a stable name.
                            digest = hashlib.sha256(
                                str(constraint.sqltext).encode()
                            ).hexdigest()[:16]
                            constraint.name = f"ck_preserved_{table_name}_{digest}"
                for constraint in declared.constraints:
                    if (
                        isinstance(constraint, CheckConstraint)
                        and isinstance(constraint.name, str)
                        and constraint.name in _OWNED_CHECKS[table_name]
                    ):
                        reflected.append_constraint(
                            CheckConstraint(constraint.sqltext, name=constraint.name)
                        )
                indexes, triggers = extensions[table_name]
                with ops.batch_alter_table(
                    table_name, recreate="always", copy_from=reflected
                ) as batch:
                    for name in names:
                        batch.alter_column(
                            name, type_=Text(), existing_type=reflected.c[name].type
                        )
                existing_indexes = set(
                    connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
                        (table_name,),
                    ).scalars()
                )
                for identity, definition in indexes:
                    if identity not in existing_indexes:
                        connection.exec_driver_sql(definition)
                for definition in triggers:
                    if not isinstance(definition, str):
                        raise TypeError("unreadable SQLite trigger definition")
                    connection.exec_driver_sql(definition)
        if (
            connection.dialect.name == "sqlite"
            and connection.exec_driver_sql("PRAGMA foreign_key_check").first()
            is not None
        ):
            raise ValueError("exact integer adoption would damage SQLite foreign keys")


@dataclass
class _SQLiteAdoptionDeadline:
    expires_at: float
    interrupted: bool = False

    def progress(self) -> int:
        self.interrupted = self.interrupted or time.monotonic() >= self.expires_at
        return int(self.interrupted)


def reconcile_exact_integer_schema(engine: Engine) -> None:
    """The platform retries transient SQLite contention within one deadline.

    Each failed attempt rolls back and removes its progress callback before a
    fresh checkout. Our deadline interruption, integrity and contract refusals
    propagate; only contention or an unrelated native interrupt can retry.
    """
    deadline = _SQLiteAdoptionDeadline(
        time.monotonic() + DATABASE_WAIT_BUDGETS.transaction_timeout_ms / 1000
    )
    for attempt in range(3):
        try:
            _reconcile_exact_integer_schema_once(engine, deadline)
            return
        except DBAPIError as error:
            code = getattr(error.orig, "sqlite_errorcode", None)
            remaining = deadline.expires_at - time.monotonic()
            if (
                deadline.interrupted
                or remaining <= 0
                or not isinstance(error.orig, sqlite3.OperationalError)
                or not isinstance(code, int)
                or code & 0xFF
                not in {
                    sqlite3.SQLITE_INTERRUPT,
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                }
                or attempt == 2
            ):
                raise
            time.sleep(min(0.1 * (attempt + 1), remaining))
            # Sleep/scheduling can consume the last of the budget. Preserve
            # the original failure instead of starting an expired attempt.
            if time.monotonic() >= deadline.expires_at:
                raise


def _reconcile_exact_integer_schema_once(
    engine: Engine, deadline: _SQLiteAdoptionDeadline
) -> None:
    """SQLite-only isolated startup entry; never nest in an active service tx.

    Foreign-key settings belong to this checked-out connection and are restored
    outside the owning transaction on every exit. No pool-wide setting changes.
    """
    if engine.dialect.name != "sqlite":
        raise ValueError("isolated exact integer schema entry is SQLite-only")
    with engine.connect() as connection:
        # A current/fresh engine needs only read-only owning metadata inspection:
        # no write lock, pragma toggle, callback or unrelated FK scan.
        if not _pending_adoption(connection):
            return
        driver = connection.connection.driver_connection
        if not isinstance(driver, sqlite3.Connection):
            raise TypeError(
                "SQLite schema entry requires its native exclusive connection"
            )
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        legacy_alter = connection.exec_driver_sql(
            "PRAGMA legacy_alter_table"
        ).scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        connection.commit()
        try:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
            remaining_ms = max(0, int((deadline.expires_at - time.monotonic()) * 1000))
            lock_timeout_ms = min(DATABASE_WAIT_BUDGETS.lock_timeout_ms, remaining_ms)
            connection.exec_driver_sql(f"PRAGMA busy_timeout={lock_timeout_ms}")
            connection.commit()
            # An exclusive startup checkout owns this callback. The interval
            # is polling cadence; the bound comes from the transaction budget.
            driver.set_progress_handler(deadline.progress, 1000)
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            if (
                connection.exec_driver_sql("PRAGMA foreign_key_check").first()
                is not None
            ):
                raise ValueError("existing SQLite foreign-key damage prevents adoption")
            adopt_exact_integer_columns(connection)
            connection.commit()
        except BaseException:
            # An expired callback must not interrupt the rollback itself.
            driver.set_progress_handler(None, 0)
            connection.rollback()
            raise
        finally:
            driver.set_progress_handler(None, 0)
            connection.exec_driver_sql(f"PRAGMA foreign_keys={int(foreign_keys)}")
            connection.exec_driver_sql(f"PRAGMA legacy_alter_table={int(legacy_alter)}")
            connection.exec_driver_sql(f"PRAGMA busy_timeout={int(busy_timeout)}")
            connection.commit()
