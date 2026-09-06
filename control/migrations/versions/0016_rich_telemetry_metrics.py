"""Persist schema-2 per-device and per-run telemetry observations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_rich_telemetry_metrics"
down_revision = "0015_model_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "node_telemetry_samples",
        sa.Column(
            "metrics",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    # Existing rollup rows predate the rich contract.  Keep them readable with
    # explicit legacy provenance while every newly written row carries the
    # complete series identity below.
    for name, column in (
        ("key", sa.Column("key", sa.String(length=96), nullable=True)),
        ("scope", sa.Column("scope", sa.String(length=16), nullable=True)),
        ("device_id", sa.Column("device_id", sa.String(length=128), nullable=True)),
        ("process_id", sa.Column("process_id", sa.BigInteger(), nullable=True)),
        ("process_name", sa.Column("process_name", sa.String(length=128), nullable=True)),
        ("interface_name", sa.Column("interface_name", sa.String(length=64), nullable=True)),
        ("run_id", sa.Column("run_id", sa.String(length=128), nullable=True)),
        (
            "unit",
            sa.Column(
                "unit", sa.String(length=32), nullable=False, server_default="unknown"
            ),
        ),
        (
            "source",
            sa.Column(
                "source", sa.String(length=128), nullable=False, server_default="legacy"
            ),
        ),
        (
            "measurement_kind",
            sa.Column(
                "measurement_kind",
                sa.String(length=16),
                nullable=False,
                server_default="measured",
            ),
        ),
        (
            "aggregation",
            sa.Column(
                "aggregation", sa.String(length=32), nullable=False, server_default="mean"
            ),
        ),
    ):
        op.add_column("node_telemetry_rollup_metrics", column)


def downgrade() -> None:
    for name in (
        "aggregation",
        "measurement_kind",
        "source",
        "unit",
        "run_id",
        "interface_name",
        "process_name",
        "process_id",
        "device_id",
        "scope",
        "key",
    ):
        op.drop_column("node_telemetry_rollup_metrics", name)
    op.drop_column("node_telemetry_samples", "metrics")
