"""Detect a test that retypes a column of the shared ORM metadata.

Retyping ``Base.metadata`` columns (even if restored) leaves SQLAlchemy's
memoized bind types stale on the ORM attributes, so an unrelated later test on
the same worker fails (``text <> bigint``, #1968). Prove a historical column
type on a private ``MetaData`` or a private engine dialect instead. The
autouse fixture in ``conftest.py`` fails any test that changes a column type.
"""

from __future__ import annotations

from sqlalchemy import MetaData


def snapshot(metadata: MetaData) -> dict[str, type]:
    return {
        f"{table.name}.{column.name}": type(column.type)
        for table in metadata.tables.values()
        for column in table.columns
    }


def drift(metadata: MetaData, before: dict[str, type]) -> list[str]:
    """Columns whose type class differs from the snapshot."""
    return [
        f"{name}: {before[name].__name__} -> {now.__name__}"
        for name, now in snapshot(metadata).items()
        if name in before and before[name] is not now
    ]
