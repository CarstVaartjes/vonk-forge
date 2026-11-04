"""Db: report live keys and uniqueness that differ from the model.

Reconciliation installs what it safely can; whatever still differs afterwards
(a key whose rows conflict, a uniqueness rule existing rows break, a live rule
the model no longer declares) is reported once per startup as a typed warning.
The report never fails startup and never changes the schema: the next startup
reconciles again once the rows allow it.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from .constants import _LOGGER
from .schema_helpers import (
    _TOLERATED_SCHEMA_DIFFERENCES,
    _constraint_kind,
    _flatten_schema_differences,
    _schema_difference_key,
)


class SchemaDriftKind(StrEnum):
    MISSING_FOREIGN_KEY = "missing_foreign_key"
    UNDECLARED_FOREIGN_KEY = "undeclared_foreign_key"
    MISSING_UNIQUE_CONSTRAINT = "missing_unique_constraint"
    UNDECLARED_UNIQUE_CONSTRAINT = "undeclared_unique_constraint"


_DRIFT_KIND: dict[tuple[str, str | None], SchemaDriftKind] = {
    ("add_fk", "foreignkey"): SchemaDriftKind.MISSING_FOREIGN_KEY,
    ("remove_fk", "foreignkey"): SchemaDriftKind.UNDECLARED_FOREIGN_KEY,
    ("add_constraint", "unique"): SchemaDriftKind.MISSING_UNIQUE_CONSTRAINT,
    ("remove_constraint", "unique"): SchemaDriftKind.UNDECLARED_UNIQUE_CONSTRAINT,
}


@dataclass(frozen=True)
class SchemaDrift:
    kind: SchemaDriftKind
    table: str
    constraint: str | None
    columns: tuple[str, ...]


def _columns(constraint: Any) -> tuple[str, ...]:
    if isinstance(constraint, ForeignKeyConstraint):
        return tuple(constraint.column_keys)
    return tuple(column.name for column in constraint.columns)


def constraint_drift(connection: Connection) -> list[SchemaDrift]:
    """Model-owned foreign keys and unique constraints the live catalog differs on."""

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from ..models import Base

    drift: list[SchemaDrift] = []
    for difference in _flatten_schema_differences(
        compare_metadata(MigrationContext.configure(connection), Base.metadata)
    ):
        if _schema_difference_key(difference) in _TOLERATED_SCHEMA_DIFFERENCES:
            continue
        constraint = difference[1]
        kind = _DRIFT_KIND.get((str(difference[0]), _constraint_kind(constraint)))
        table = getattr(getattr(constraint, "table", None), "name", None)
        if (
            kind is None
            or not isinstance(table, str)
            or table not in Base.metadata.tables
        ):
            continue
        drift.append(SchemaDrift(kind, table, constraint.name, _columns(constraint)))
    return drift


def report_constraint_drift(connection: Connection) -> list[SchemaDrift]:
    """Log each remaining drift as one warning; a failed check only defers it."""

    try:
        with connection.begin_nested():
            drift = constraint_drift(connection)
    except SQLAlchemyError:
        _LOGGER.exception("Schema drift check deferred to the next startup")
        return []
    for item in drift:
        _LOGGER.warning(
            "Schema drift %s on %s: %s (%s)",
            item.kind.value,
            item.table,
            item.constraint or "unnamed",
            ", ".join(item.columns),
            extra={"schema_drift": item.kind.value},
        )
    return drift
