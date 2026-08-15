"""Add durable telemetry maintenance fairness state."""

import sqlalchemy as sa
from alembic import op

revision = "0026_telemetry_maintenance_state"
down_revision = "0025_telemetry_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    state = op.create_table(
        "telemetry_maintenance_state",
        sa.Column("singleton_id", sa.SmallInteger, primary_key=True),
        sa.Column("next_resolution_seconds", sa.SmallInteger, nullable=False),
        sa.CheckConstraint(
            "singleton_id = 1",
            name="ck_telemetry_maintenance_state_singleton",
        ),
        sa.CheckConstraint(
            "next_resolution_seconds IN (60, 900)",
            name="ck_telemetry_maintenance_state_resolution",
        ),
    )
    op.bulk_insert(
        state,
        [{"singleton_id": 1, "next_resolution_seconds": 60}],
    )


def downgrade() -> None:
    op.drop_table("telemetry_maintenance_state")
