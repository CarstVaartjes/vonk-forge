"""Remove agent-owned telemetry sequence bookkeeping."""

from __future__ import annotations

from alembic import op

revision = "0025_remove_telemetry_sequence"
down_revision = "0024_failure_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("node_telemetry_samples") as batch:
        batch.drop_constraint("uq_telemetry_node_boot_sequence", type_="unique")
        batch.drop_constraint("ck_telemetry_sequences", type_="check")
        batch.drop_column("sequence")
        batch.create_unique_constraint(
            "uq_telemetry_node_boot_observed",
            ["node_id", "boot_id", "observed_at"],
        )
        batch.create_check_constraint(
            "ck_telemetry_gap_samples",
            "gap_samples BETWEEN 0 AND 9223372036854775807",
        )


def downgrade() -> None:
    raise RuntimeError("telemetry sequence bookkeeping is not supported")
