"""Add durable telemetry rollups and dirty maintenance work."""

import sqlalchemy as sa
from alembic import op

revision = "0025_telemetry_retention"
down_revision = "0024_fleet_stream_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_telemetry_rollup_buckets",
        sa.Column("resolution_seconds", sa.SmallInteger, primary_key=True),
        sa.Column(
            "node_id",
            sa.String(36),
            sa.ForeignKey(
                "agent_nodes.node_id",
                name="fk_telemetry_rollup_buckets_node",
                ondelete="CASCADE",
            ),
            primary_key=True,
        ),
        sa.Column(
            "bucket_start",
            sa.DateTime(timezone=True),
            primary_key=True,
        ),
        sa.Column("source_sample_count", sa.BigInteger, nullable=False),
        sa.Column("gap_samples", sa.BigInteger, nullable=False),
        sa.CheckConstraint(
            "resolution_seconds IN (60, 900)",
            name="ck_telemetry_rollup_buckets_resolution",
        ),
        sa.CheckConstraint(
            "source_sample_count BETWEEN 0 AND 9223372036854775807 AND "
            "gap_samples BETWEEN 0 AND 9223372036854775807",
            name="ck_telemetry_rollup_buckets_counts",
        ),
    )
    op.create_index(
        "ix_telemetry_rollup_buckets_resolution_start",
        "node_telemetry_rollup_buckets",
        ["resolution_seconds", "bucket_start", "node_id"],
    )
    op.create_table(
        "node_telemetry_rollup_metrics",
        sa.Column("resolution_seconds", sa.SmallInteger, primary_key=True),
        sa.Column("node_id", sa.String(36), primary_key=True),
        sa.Column(
            "bucket_start",
            sa.DateTime(timezone=True),
            primary_key=True,
        ),
        sa.Column("metric_name", sa.String(64), primary_key=True),
        sa.Column("sample_count", sa.BigInteger, nullable=False),
        sa.Column("minimum", sa.Float, nullable=False),
        sa.Column("mean", sa.Float, nullable=False),
        sa.Column("maximum", sa.Float, nullable=False),
        sa.ForeignKeyConstraint(
            ("resolution_seconds", "node_id", "bucket_start"),
            (
                "node_telemetry_rollup_buckets.resolution_seconds",
                "node_telemetry_rollup_buckets.node_id",
                "node_telemetry_rollup_buckets.bucket_start",
            ),
            name="fk_telemetry_rollup_metrics_bucket",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "resolution_seconds IN (60, 900)",
            name="ck_telemetry_rollup_metrics_resolution",
        ),
        sa.CheckConstraint(
            "length(metric_name) BETWEEN 1 AND 64",
            name="ck_telemetry_rollup_metrics_name",
        ),
        sa.CheckConstraint(
            "sample_count BETWEEN 0 AND 9223372036854775807",
            name="ck_telemetry_rollup_metrics_count",
        ),
        sa.CheckConstraint(
            "minimum BETWEEN -1e308 AND 1e308 AND "
            "mean BETWEEN -1e308 AND 1e308 AND "
            "maximum BETWEEN -1e308 AND 1e308 AND "
            "minimum <= mean AND mean <= maximum",
            name="ck_telemetry_rollup_metrics_values",
        ),
    )
    op.create_table(
        "node_telemetry_rollup_dirty",
        sa.Column("resolution_seconds", sa.SmallInteger, primary_key=True),
        sa.Column(
            "node_id",
            sa.String(36),
            sa.ForeignKey(
                "agent_nodes.node_id",
                name="fk_telemetry_rollup_dirty_node",
                ondelete="CASCADE",
            ),
            primary_key=True,
        ),
        sa.Column(
            "bucket_start",
            sa.DateTime(timezone=True),
            primary_key=True,
        ),
        sa.CheckConstraint(
            "resolution_seconds IN (60, 900)",
            name="ck_telemetry_rollup_dirty_resolution",
        ),
    )
    op.create_index(
        "ix_telemetry_rollup_dirty_resolution_start",
        "node_telemetry_rollup_dirty",
        ["resolution_seconds", "bucket_start", "node_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_telemetry_rollup_dirty_resolution_start",
        table_name="node_telemetry_rollup_dirty",
    )
    op.drop_table("node_telemetry_rollup_dirty")
    op.drop_table("node_telemetry_rollup_metrics")
    op.drop_index(
        "ix_telemetry_rollup_buckets_resolution_start",
        table_name="node_telemetry_rollup_buckets",
    )
    op.drop_table("node_telemetry_rollup_buckets")
