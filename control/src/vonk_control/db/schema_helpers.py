"""Db: schema helpers."""

import re
import uuid
from enum import StrEnum
from importlib import metadata
from typing import Any

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.operations.ops import CreateForeignKeyOp
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    DefaultClause,
    ForeignKeyConstraint,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
    inspect,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import (
    SQLAlchemyError,
)
from sqlalchemy.schema import CreateTable, DropTable

from vonk_control import (
    models as _models,  # noqa: F401 -- loads every mapped table onto Base.metadata
)
from vonk_control.db.connections import _database_sqlstate, _reraise_contention
from vonk_control.db.constants import _LOGGER
from vonk_control.model_primitives import Base

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


# Tables in the Controller's schema that are not model tables and must stay.
# LiteLLM keeps its own database (postgres/init-databases.sh), so only the
# migration marker lives here.
_FOREIGN_TABLES = frozenset({"alembic_version"})


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
                    f"EXPLAIN (VERBOSE, FORMAT JSON) SELECT ({expression}) FROM {quoted_table}"  # noqa: S608 -- interpolates identifiers, types or placeholder styles, which cannot be bound
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
                except SQLAlchemyError as error:
                    _reraise_contention(error)
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
                _reraise_contention(error)
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
                        # The failed replacement rolled back its drop too: a
                        # changed CHECK is replaced here, not left behind.
                        if mismatch:
                            ops.drop_constraint(name, table_name, type_="check")
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
                    _reraise_contention(deferred_error)
                    _LOGGER.warning(
                        "Could not create CHECK %s.%s: %s",
                        table_name,
                        name,
                        deferred_error,
                    )


#: The comment a kept undeclared table carries: the Controller minor that first
#: saw it undeclared. The table is the only place this fact lives.
_UNDECLARED_STAMP = "vonk-undeclared-since "

_STAMP_PATTERN = re.compile(rf"{_UNDECLARED_STAMP}(\d+)\.(\d+)")

Minor = tuple[int, int]


def _controller_minor() -> Minor | None:
    """The ``(major, minor)`` of this Controller's release; ``None`` when unknown."""

    try:
        found = re.match(r"(\d+)\.(\d+)", metadata.version("vonk-control"))
    except metadata.PackageNotFoundError:
        return None
    return (int(found[1]), int(found[2])) if found else None


_CONTROLLER_MINOR = _controller_minor()


class UndeclaredTableAction(StrEnum):
    """What one reconciliation does with a table the model does not declare."""

    STAMP = "stamp"
    KEEP = "keep"
    DROP = "drop"


def _undeclared_table_action(
    stamp: str | None, current: Minor
) -> UndeclaredTableAction:
    """The one transition for an undeclared table.

    unstamped -> STAMP; stamped with this minor or newer -> KEEP; stamped with
    an older minor -> DROP; an unparseable stamp is stamped again.
    """

    found = _STAMP_PATTERN.fullmatch(stamp) if stamp else None
    if found is None:
        return UndeclaredTableAction.STAMP
    if current > (int(found[1]), int(found[2])):
        return UndeclaredTableAction.DROP
    return UndeclaredTableAction.KEEP


def _undeclared_stamps(connection: Connection) -> dict[str, str]:
    """The tables of the current schema that carry an undeclared stamp."""

    return {
        name: comment
        for name, comment in connection.exec_driver_sql(
            "SELECT c.relname, obj_description(c.oid, 'pg_class') FROM pg_class c "
            "WHERE c.relkind = 'r' AND c.relnamespace = current_schema()::regnamespace"
        )
        if comment and comment.startswith(_UNDECLARED_STAMP)
    }


def _clear_declared_stamps(connection: Connection) -> None:
    """Clear the stamp of a table the model declares again.

    Runs before the differences are read, so the stale comment is not reported
    as drift.
    """

    if connection.dialect.name != "postgresql":
        return
    quote = connection.dialect.identifier_preparer.quote
    for table_name in sorted(
        _undeclared_stamps(connection).keys() & set(Base.metadata.tables)
    ):
        connection.exec_driver_sql(f"COMMENT ON TABLE {quote(table_name)} IS NULL")
        _LOGGER.info(
            "Table %s is declared again; cleared its undeclared stamp", table_name
        )


def _reconcile_undeclared_tables(connection: Connection, ops: Any) -> None:
    """Keep every table the model does not declare for one minor release.

    The rule, not a list: a table the code does not declare is nothing the
    Controller reads or writes, but a rollback to the previous minor must find
    the tables the newer one added. Each is stamped with the minor that first
    saw it undeclared and dropped by the first Controller of a later minor.
    Foreign keys go first so a RESTRICT key cannot block a delete; the drop has
    no CASCADE, so a table a declared table still references is refused, logged
    and retried on the next start.
    """

    if connection.dialect.name != "postgresql":
        return

    inspector = inspect(connection)
    declared = set(Base.metadata.tables)
    undeclared = sorted(set(inspector.get_table_names()) - declared - _FOREIGN_TABLES)
    stamps = _undeclared_stamps(connection)
    quote = connection.dialect.identifier_preparer.quote
    for table_name in undeclared:
        for key in inspector.get_foreign_keys(table_name):
            name = key["name"]
            if not name:
                continue
            try:
                with connection.begin_nested():
                    ops.drop_constraint(name, table_name, type_="foreignkey")
            except SQLAlchemyError as error:
                _reraise_contention(error)
                _LOGGER.warning(
                    "Could not drop foreign key %s.%s: %s", table_name, name, error
                )
    current = _CONTROLLER_MINOR
    if current is None:
        return  # unknown version: keep every table, drop none
    for table_name in undeclared:
        action = _undeclared_table_action(stamps.get(table_name), current)
        try:
            with connection.begin_nested():
                if action is UndeclaredTableAction.DROP:
                    ops.drop_table(table_name)
                    _LOGGER.info("Dropped undeclared table %s", table_name)
                elif action is UndeclaredTableAction.STAMP:
                    connection.exec_driver_sql(
                        f"COMMENT ON TABLE {quote(table_name)} "
                        f"IS '{_UNDECLARED_STAMP}{current[0]}.{current[1]}'"
                    )
                    _LOGGER.warning(
                        "Kept undeclared table %s until the next minor", table_name
                    )
        except SQLAlchemyError as error:
            _reraise_contention(error)
            _LOGGER.warning(
                "Could not reconcile undeclared table %s: %s", table_name, error
            )


class SchemaDriftKind(StrEnum):
    """What a startup reconciliation left different from the model."""

    MISSING_FOREIGN_KEY = "missing_foreign_key"
    UNDECLARED_FOREIGN_KEY = "undeclared_foreign_key"
    MISSING_UNIQUE_CONSTRAINT = "missing_unique_constraint"
    UNDECLARED_UNIQUE_CONSTRAINT = "undeclared_unique_constraint"
    DEFERRED_CHECK_CONSTRAINT = "deferred_check_constraint"
    UNHANDLED_DIFFERENCE = "unhandled_difference"


SchemaDrift = dict[SchemaDriftKind, list[str]]


# Every Alembic difference kind reconciliation has a rule for. Tables the model
# no longer declares are kept one minor and then dropped, columns it no longer declares are kept with
# their data, and an obsolete index stays until its replacement exists; those are decisions, not
# drift. Any other kind is reported as unhandled.
_RECONCILED_DIFFERENCES = frozenset(
    {
        "add_table",
        "remove_table",
        "add_column",
        "remove_column",
        "modify_type",
        "modify_nullable",
        "modify_default",
        "add_index",
        "remove_index",
        "add_constraint",
        "remove_constraint",
        "add_fk",
        "remove_fk",
    }
)


def _constraint_label(constraint: Any) -> str:
    columns = getattr(constraint, "column_keys", None) or [
        column.name for column in constraint.columns
    ]
    return (
        f"{constraint.table.name}.{constraint.name or 'unnamed'}({', '.join(columns)})"
    )


def _unhandled_differences(differences: list[tuple[Any, ...]]) -> list[str]:
    """Differences no reconciliation rule acts on, named for the report."""

    return [
        "{}:{}".format(*_schema_difference_key(difference))
        for difference in differences
        if difference[0] not in _RECONCILED_DIFFERENCES
    ]


def _install_foreign_keys(
    connection: Connection,
    ops: Any,
    differences: list[tuple[Any, ...]],
    drift: SchemaDrift,
) -> None:
    """Install model foreign keys the live catalog lacks or holds differently.

    Alembic reports a missing key as ``add_fk``; a live key on the same columns
    whose target or delete rule differs comes as a ``remove_fk`` of the old form.
    The old form is dropped only in the savepoint that installs its
    replacement. When existing rows violate the key (SQLSTATE 23503, for
    example measurements left behind by a missing cascade), PostgreSQL gets it
    ``NOT VALID``: it still checks new rows and still runs its ``ON DELETE``
    rule, while the owners of the old rows delete them. A busy relation
    (55P03) is raised to the startup retry; any other refusal is reported as
    drift and tried again at the next startup.
    """

    replaced: dict[tuple[str, frozenset[str]], list[Any]] = {}
    for difference in differences:
        if difference[0] == "remove_fk":
            key = (difference[1].table.name, frozenset(difference[1].column_keys))
            replaced.setdefault(key, []).append(difference[1])
    installed: set[tuple[str, frozenset[str]]] = set()
    for difference in differences:
        if difference[0] != "add_fk":
            continue
        constraint = difference[1]
        key = (constraint.table.name, frozenset(constraint.column_keys))
        old_names = [old.name for old in replaced.get(key, []) if old.name]
        label = _constraint_label(constraint)
        try:
            _create_foreign_key(connection, ops, constraint, old_names, not_valid=False)
            _LOGGER.info("Created foreign key %s", label)
            installed.add(key)
            continue
        except SQLAlchemyError as error:
            _reraise_contention(error)
            if _database_sqlstate(error) != "23503":
                _LOGGER.warning("Could not create foreign key %s: %s", label, error)
                drift[SchemaDriftKind.MISSING_FOREIGN_KEY].append(label)
                continue
            violation = error
        try:
            _create_foreign_key(connection, ops, constraint, old_names, not_valid=True)
        except SQLAlchemyError as error:
            _reraise_contention(error)
            _LOGGER.warning("Could not create foreign key %s: %s", label, error)
            drift[SchemaDriftKind.MISSING_FOREIGN_KEY].append(label)
            continue
        _LOGGER.warning(
            "Created foreign key %s NOT VALID because existing rows violate it: %s",
            label,
            violation,
        )
        installed.add(key)
    for key, olds in replaced.items():
        if key not in installed:
            drift[SchemaDriftKind.UNDECLARED_FOREIGN_KEY].extend(
                _constraint_label(old) for old in olds
            )


def _create_foreign_key(
    connection: Connection,
    ops: Any,
    constraint: Any,
    old_names: list[str],
    *,
    not_valid: bool,
) -> None:
    """Replace ``old_names`` with ``constraint`` in one savepoint."""

    operation = CreateForeignKeyOp.from_constraint(constraint)
    if not_valid:
        operation.kw["postgresql_not_valid"] = True
    with connection.begin_nested():
        for name in old_names:
            ops.drop_constraint(name, constraint.table.name, type_="foreignkey")
        ops.invoke(operation)
