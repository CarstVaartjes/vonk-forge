"""The startup schema reconcile tightens a state CHECK to the core vocabulary.

The previous release admitted the retired ``partial`` in its model-cache
operations CHECK.  Startup must replace that constraint with the core words
without refusing to start: with no row carrying the retired word the new CHECK
is fully valid, and a row that still carries it is kept, untouched, under a
``NOT VALID`` constraint that refuses every new write of the word.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import Table, text
from sqlalchemy.exc import IntegrityError
from vonk_agent_protocol import LifecycleState
from vonk_control.db import reconcile_schema
from vonk_control.models import Base, ModelCacheOperation

CHECK = "ck_model_cache_operations_state"
#: The constraint the previous release created.
PREVIOUS_CHECK = (
    "state IN ('queued','running','backoff','succeeded','failed','cancelled','partial')"
)


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


def _previous_release(connection) -> None:
    table = cast(Table, ModelCacheOperation.__table__)
    Base.metadata.create_all(connection)
    connection.execute(text(f"ALTER TABLE {table.name} DROP CONSTRAINT {CHECK}"))
    connection.execute(
        text(
            f"ALTER TABLE {table.name} ADD CONSTRAINT {CHECK} CHECK ({PREVIOUS_CHECK})"
        )
    )


def _validated(connection) -> bool:
    return bool(
        connection.execute(
            text("SELECT convalidated FROM pg_constraint WHERE conname = :name"),
            {"name": CHECK},
        ).scalar_one()
    )


@pytest.mark.parametrize("retired_row", [False, True], ids=["clean", "retired-row"])
def test_startup_tightens_the_state_check_and_never_refuses(
    postgres_engine, retired_row: bool
):
    table = cast(Table, ModelCacheOperation.__table__)
    with postgres_engine.begin() as connection:
        _previous_release(connection)
        _insert(connection, "00000000-0000-4000-8000-000000000001", "backoff")
        if retired_row:
            _insert(connection, "00000000-0000-4000-8000-000000000002", "partial")

    with postgres_engine.begin() as connection:
        reconcile_schema(connection)  # startup does not refuse either database

    with postgres_engine.begin() as connection:
        # A clean database gets a fully valid constraint; a retired row is kept.
        assert _validated(connection) is not retired_row
        states = connection.execute(
            text(f"SELECT state FROM {table.name} ORDER BY id")
        ).scalars()
        assert list(states) == ["backoff", "partial"][: 2 if retired_row else 1]
        with pytest.raises(IntegrityError), connection.begin_nested():
            _insert(connection, "00000000-0000-4000-8000-000000000003", "partial")
        fresh_id = "00000000-0000-4000-8000-000000000004"
        _insert(connection, fresh_id, LifecycleState.BACKOFF)
        recovered = connection.execute(
            table.select().where(table.c.id == fresh_id)
        ).one()
        assert recovered.state == LifecycleState.BACKOFF
