"""Db: schema."""

import copy

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import (
    SQLAlchemyError,
)

from .constants import _LOGGER
from .schema_helpers import (
    _REPLACED_CONSTRAINT_NAMES,
    _TOLERATED_SCHEMA_DIFFERENCES,
    _check_constraint_differences,
    _column_default_sql,
    _constraint_kind,
    _cosmetic_type_difference,
    _flatten_schema_differences,
    _is_generated_sequence_default,
    _quoted,
    _release_retired_table_foreign_keys,
    _repair_check_constraints,
    _schema_difference_key,
)


def reconcile_schema(connection: Connection) -> None:
    """Apply safe metadata differences inside the caller's startup transaction."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    from ..exact_integer_adoption import adopt_exact_integer_columns
    from ..models import Base

    adopt_exact_integer_columns(connection)
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
                        if (table_name, column.name) == (
                            "artifact_distribution_assignments",
                            "oci_image_config_digest",
                        ):
                            # No stored manifest proves the missing config identity.
                            # Retire these unusable grants so normal distribution
                            # can recreate them from verified evidence. Keep this
                            # atomic with tightening; never invent a digest.
                            connection.execute(table.delete().where(column.is_(None)))
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
