"""One dialect-aware ``INSERT`` for upserts that run on PostgreSQL and SQLite."""

from __future__ import annotations

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session


def dialect_insert(session: Session, model: type) -> postgresql.Insert | sqlite.Insert:
    """``insert(model)`` with ``on_conflict_*`` for the session's dialect."""
    if session.get_bind().dialect.name == "postgresql":
        return postgresql.insert(model)
    return sqlite.insert(model)
