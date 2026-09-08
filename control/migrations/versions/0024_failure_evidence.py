"""Current bounded failure evidence and incremental collector cursors."""

import sqlalchemy as sa
from alembic import op

revision = "0024_failure_evidence"
down_revision = "0023_recipe_route_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "failure_evidence_records",
        sa.Column("operation_id", sa.String(128), primary_key=True),
        sa.Column("attempt", sa.Integer, primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content", sa.LargeBinary, nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_failure_evidence_records_collected_at",
        "failure_evidence_records",
        ["collected_at"],
    )
    op.create_table(
        "failure_evidence_cursors",
        sa.Column("family", sa.String(32), primary_key=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("failure_evidence_cursors")
    op.drop_index(
        "ix_failure_evidence_records_collected_at",
        table_name="failure_evidence_records",
    )
    op.drop_table("failure_evidence_records")
