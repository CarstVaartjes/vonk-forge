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
from vonk_control.telemetry_maintenance import (
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


def _fill(connection, table: str, extra_columns: str, extra_values: str) -> None:
    connection.execute(
        text(
            f"INSERT INTO {table} (id, node_id, observed_at{extra_columns}) "
            "SELECT md5(g::text), 'node-' || (g % 3), "
            "timestamptz '2026-11-01' + g * interval '15 seconds'"
            f"{extra_values} FROM generate_series(1, 20000) AS g"
        )
    )
    connection.execute(text(f"ANALYZE {table}"))


def test_prune_selections_use_their_indexes_and_startup_adds_them(postgres_engine):
    with postgres_engine.begin() as connection:
        Base.metadata.create_all(connection)
        for name in _INDEXES:
            connection.execute(text(f"DROP INDEX {name}"))
    with postgres_engine.begin() as connection:
        reconcile_schema(connection)
        # Representative rows, without the unrelated required columns.
        connection.execute(text("SET LOCAL session_replication_role = replica"))
        for table in ("node_inventory_snapshots", "node_telemetry_samples"):
            for (name,) in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    f"WHERE conrelid = '{table}'::regclass AND contype = 'c'"
                )
            ).all():
                connection.execute(
                    text(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
                )
        _fill(
            connection,
            "node_inventory_snapshots",
            ", kind, received_at, evidence_digest",
            ", 'report', now(), md5(g::text) || md5(g::text || 'x')",
        )
        for (column,) in connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'node_telemetry_samples' AND is_nullable = 'NO' "
                "AND column_name NOT IN ('id', 'node_id', 'observed_at')"
            )
        ).all():
            connection.execute(
                text(
                    "ALTER TABLE node_telemetry_samples "
                    f'ALTER COLUMN "{column}" DROP NOT NULL'
                )
            )
        connection.execute(
            text(
                "INSERT INTO node_telemetry_samples (id, node_id, observed_at, "
                "received_at) SELECT md5(g::text), 'node-' || (g % 3), "
                "timestamptz '2026-11-01' + g * interval '15 seconds', now() "
                "FROM generate_series(1, 20000) AS g"
            )
        )
        connection.execute(text("ANALYZE node_telemetry_samples"))
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
        assert "Index" in plan, (name, plan)
