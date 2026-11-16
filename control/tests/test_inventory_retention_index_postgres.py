"""Inventory retention reads expired reports through an index on ``observed_at``.

Every maintenance pass selects the oldest expired reports.  Without an index
leading on ``observed_at`` it read and sorted every retained report.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from vonk_control.db import reconcile_schema
from vonk_control.models import Base, NodeInventorySnapshot

pytestmark = pytest.mark.postgres

_INDEX = "ix_inventory_report_observed"


def _prune_selection() -> str:
    statement = (
        select(NodeInventorySnapshot.id)
        .where(NodeInventorySnapshot.observed_at < datetime(2026, 1, 1, tzinfo=UTC))
        .order_by(NodeInventorySnapshot.observed_at, NodeInventorySnapshot.id)
        .limit(500)
    )
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_startup_adds_the_index_and_the_prune_selection_uses_it(postgres_engine):
    with postgres_engine.begin() as connection:
        Base.metadata.create_all(connection)
        connection.execute(text(f"DROP INDEX {_INDEX}"))
    with postgres_engine.begin() as connection:
        reconcile_schema(connection)
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            row[0] for row in connection.execute(text("EXPLAIN " + _prune_selection()))
        )
    assert _INDEX in plan, plan
    assert "Sort" not in plan, plan
