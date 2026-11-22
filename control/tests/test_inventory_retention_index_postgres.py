"""Retention reads expired rows through an index on ``observed_at``.

Every maintenance pass selects the oldest expired rows.  Without an index
leading on ``observed_at`` it read and sorted every retained row.  This test
plans the real statements, so a new unindexed prune selection is added here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from vonk_control.db import reconcile_schema
from vonk_control.models import Base
from vonk_control.telemetry.maintenance import (
    _expired_inventory_statement,
    _raw_candidate_statement,
)

pytestmark = pytest.mark.postgres

_CUTOFF = datetime(2026, 11, 10, tzinfo=UTC)
_INDEXES = ("ix_inventory_report_observed", "ix_telemetry_observed_node_id")


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def _plan(connection, statement) -> str:
    return "\n".join(
        row[0] for row in connection.execute(text("EXPLAIN " + _sql(statement)))
    )


def test_prune_selections_use_their_indexes_and_startup_adds_them(postgres_engine):
    with postgres_engine.begin() as connection:
        Base.metadata.create_all(connection)
        for name in _INDEXES:
            connection.execute(text(f"DROP INDEX {name}"))
    with postgres_engine.begin() as connection:
        reconcile_schema(connection)
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        connection.execute(text("SET LOCAL enable_bitmapscan = off"))
        plans = {
            "inventory": _plan(
                connection,
                _expired_inventory_statement(cutoff=_CUTOFF, newest=["x"], limit=500),
            ),
            "raw": _plan(
                connection, _raw_candidate_statement(cutoff=_CUTOFF, limit=500)
            ),
        }
    for name, plan in plans.items():
        assert "Sort" not in plan, (name, plan)
