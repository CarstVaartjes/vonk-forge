"""Align current telemetry defaults without rewriting historical backfills."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_current_telemetry_defaults"
down_revision = "0021_runtime_authz"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0016 remains unchanged: its add-column backfill can run on populated
    # databases. At the current head, new samples must supply their metrics.
    # Changing these defaults does not replace any existing row values.
    with op.batch_alter_table("node_telemetry_samples") as batch:
        batch.alter_column(
            "metrics", existing_type=sa.JSON(), existing_nullable=False,
            server_default=None,
        )
    with op.batch_alter_table("node_telemetry_rollup_metrics") as batch:
        batch.alter_column(
            "source", existing_type=sa.String(128), existing_nullable=False,
            server_default="controller-derived",
        )


def downgrade() -> None:
    raise RuntimeError("The current telemetry contract is forward-only")
