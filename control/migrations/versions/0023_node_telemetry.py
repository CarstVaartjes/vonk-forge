"""Persist bounded latest and historical node telemetry."""

import sqlalchemy as sa
from alembic import op

revision = "0023_node_telemetry"
down_revision = "0022_observation_latest_index"
branch_labels = None
depends_on = None


def uuid_shape(column: str) -> str:
    compact = f"replace({column}, '-', '')"
    remainder = compact
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column}) = 36 AND substr({column}, 9, 1) = '-' AND "
        f"substr({column}, 14, 1) = '-' AND substr({column}, 19, 1) = '-' AND "
        f"substr({column}, 24, 1) = '-' AND length({compact}) = 32 AND "
        f"{compact} = lower({compact}) AND length({remainder}) = 0"
    )


def upgrade() -> None:
    op.create_table(
        "node_telemetry_samples",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "node_id",
            sa.String(36),
            sa.ForeignKey("agent_nodes.node_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("boot_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.BigInteger, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_utilization_percent", sa.Float),
        sa.Column("load_average_1m", sa.Float),
        sa.Column("memory_total_bytes", sa.BigInteger),
        sa.Column("memory_available_bytes", sa.BigInteger),
        sa.Column("disk_total_bytes", sa.BigInteger),
        sa.Column("disk_free_bytes", sa.BigInteger),
        sa.Column("gpu_utilization_percent", sa.Float),
        sa.Column("gpu_memory_total_bytes", sa.BigInteger),
        sa.Column("gpu_memory_free_bytes", sa.BigInteger),
        sa.Column("temperature_c", sa.Float),
        sa.Column("power_watts", sa.Float),
        sa.Column("network_receive_bytes_per_second", sa.Float),
        sa.Column("network_transmit_bytes_per_second", sa.Float),
        sa.Column("gap_samples", sa.BigInteger, nullable=False),
        sa.Column("details", sa.JSON, nullable=False),
        sa.CheckConstraint(uuid_shape("boot_id"), name="ck_telemetry_boot_id_shape"),
        sa.CheckConstraint(
            "sequence BETWEEN 0 AND 9223372036854775807 AND "
            "gap_samples BETWEEN 0 AND 9223372036854775807",
            name="ck_telemetry_sequences",
        ),
        sa.CheckConstraint(
            "(cpu_utilization_percent IS NULL OR cpu_utilization_percent BETWEEN 0 AND 100) "
            "AND (gpu_utilization_percent IS NULL OR "
            "gpu_utilization_percent BETWEEN 0 AND 100)",
            name="ck_telemetry_utilization",
        ),
        sa.CheckConstraint(
            "load_average_1m IS NULL OR load_average_1m BETWEEN 0 AND 1000000",
            name="ck_telemetry_load",
        ),
        sa.CheckConstraint(
            "(memory_total_bytes IS NULL AND memory_available_bytes IS NULL) OR "
            "(memory_total_bytes IS NOT NULL AND memory_available_bytes IS NOT NULL AND "
            "memory_total_bytes >= 0 AND memory_available_bytes >= 0 AND "
            "memory_available_bytes <= memory_total_bytes)",
            name="ck_telemetry_memory",
        ),
        sa.CheckConstraint(
            "(disk_total_bytes IS NULL AND disk_free_bytes IS NULL) OR "
            "(disk_total_bytes IS NOT NULL AND disk_free_bytes IS NOT NULL AND "
            "disk_total_bytes >= 0 AND disk_free_bytes >= 0 AND "
            "disk_free_bytes <= disk_total_bytes)",
            name="ck_telemetry_disk",
        ),
        sa.CheckConstraint(
            "(gpu_memory_total_bytes IS NULL AND gpu_memory_free_bytes IS NULL) OR "
            "(gpu_memory_total_bytes IS NOT NULL AND gpu_memory_free_bytes IS NOT NULL AND "
            "gpu_memory_total_bytes >= 0 AND gpu_memory_free_bytes >= 0 AND "
            "gpu_memory_free_bytes <= gpu_memory_total_bytes)",
            name="ck_telemetry_gpu_memory",
        ),
        sa.CheckConstraint(
            "(temperature_c IS NULL OR temperature_c BETWEEN -100 AND 300) "
            "AND (power_watts IS NULL OR power_watts BETWEEN 0 AND 100000) AND "
            "(network_receive_bytes_per_second IS NULL OR "
            "network_receive_bytes_per_second BETWEEN 0 AND 9223372036854775807) AND "
            "(network_transmit_bytes_per_second IS NULL OR "
            "network_transmit_bytes_per_second BETWEEN 0 AND 9223372036854775807)",
            name="ck_telemetry_physical_metrics",
        ),
        sa.CheckConstraint(
            "length(CAST(details AS TEXT)) BETWEEN 2 AND 4096",
            name="ck_telemetry_details",
        ),
        sa.UniqueConstraint(
            "node_id", "boot_id", "sequence", name="uq_telemetry_node_boot_sequence"
        ),
    )
    op.create_index(
        "ix_telemetry_node_observed",
        "node_telemetry_samples",
        ["node_id", "observed_at"],
    )
    op.create_table(
        "node_telemetry_latest",
        sa.Column(
            "node_id",
            sa.String(36),
            sa.ForeignKey("agent_nodes.node_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "sample_id",
            sa.String(36),
            sa.ForeignKey("node_telemetry_samples.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("node_telemetry_latest")
    op.drop_index("ix_telemetry_node_observed", table_name="node_telemetry_samples")
    op.drop_table("node_telemetry_samples")
