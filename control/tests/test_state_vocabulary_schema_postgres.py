"""The startup schema reconcile moves a CHECK onto the core state vocabulary.

A database written before the rename carries ``partial`` in its model-cache
operations and a CHECK constraint that does not know ``backoff``.  Startup must
bring the constraint up to date without refusing, losing or rewriting the row,
and afterwards both spellings are admitted (one release) while an unknown word is
still refused.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import Table, text
from sqlalchemy.exc import IntegrityError
from vonk_control.db import reconcile_schema
from vonk_control.models import Base, ModelCacheOperation

CHECK = "ck_model_cache_operations_state"
OLD_CHECK = "state IN ('queued','running','partial','succeeded','failed','cancelled')"


def _insert(connection, identifier: str, state: str) -> None:
    now = datetime.now(UTC)
    connection.execute(
        cast(Table, ModelCacheOperation.__table__)
        .insert()
        .values(
            id=identifier,
            request_key=identifier,
            kind="download",
            actor="test",
            state=state,
            attempt=1,
            payload={},
            progress={"schema_version": 1},
            created_at=now,
            updated_at=now,
        )
    )


def test_a_cache_database_with_partial_rows_is_reconciled_in_place(postgres_engine):
    table = cast(Table, ModelCacheOperation.__table__)
    with postgres_engine.begin() as connection:
        Base.metadata.create_all(connection)
        connection.execute(text(f"ALTER TABLE {table.name} DROP CONSTRAINT {CHECK}"))
        connection.execute(
            text(f"ALTER TABLE {table.name} ADD CONSTRAINT {CHECK} CHECK ({OLD_CHECK})")
        )
        _insert(connection, "00000000-0000-4000-8000-000000000001", "partial")
        with pytest.raises(IntegrityError), connection.begin_nested():
            _insert(connection, "00000000-0000-4000-8000-000000000002", "backoff")

    with postgres_engine.begin() as connection:
        reconcile_schema(connection)

    with postgres_engine.begin() as connection:
        # The old row is untouched; both spellings are admitted for one release.
        states = connection.execute(text(f"SELECT state FROM {table.name}")).scalars()
        assert list(states) == ["partial"]
        _insert(connection, "00000000-0000-4000-8000-000000000003", "backoff")
        _insert(connection, "00000000-0000-4000-8000-000000000004", "partial")
        with pytest.raises(IntegrityError), connection.begin_nested():
            _insert(connection, "00000000-0000-4000-8000-000000000005", "waiting")
