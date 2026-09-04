"""Persist exact Controller-to-agent artifact assignments."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_artifact_distribution_assignments"
down_revision = "0016_rich_telemetry_metrics"
branch_labels = None
depends_on = None


def _lower_hex(column: str, length: int) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"length({column}) = {length} AND {column} = lower({column}) AND "
        f"length({remainder}) = 0"
    )


def upgrade() -> None:
    op.create_table(
        "artifact_distribution_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model_artifact_set_sha256", sa.String(length=64), nullable=False),
        sa.Column("objects", sa.JSON(), nullable=False),
        sa.Column("oci_image_digest", sa.String(length=71), nullable=False),
        sa.Column("oci_archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["node_id"], ["agent_nodes.node_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_digest", "node_id", name="uq_distribution_plan_node"),
        sa.CheckConstraint(_lower_hex("plan_digest", 64), name="ck_distribution_plan_digest"),
        sa.CheckConstraint(_lower_hex("model_artifact_set_sha256", 64), name="ck_distribution_model_set_digest"),
        sa.CheckConstraint(_lower_hex("oci_archive_sha256", 64), name="ck_distribution_archive_digest"),
        sa.CheckConstraint("generation >= 1", name="ck_distribution_generation"),
        sa.CheckConstraint("state IN ('active','revoked','expired')", name="ck_distribution_state"),
    )
    op.create_index(
        "ix_artifact_distribution_assignments_plan_digest",
        "artifact_distribution_assignments",
        ["plan_digest"],
    )
    op.create_index(
        "ix_artifact_distribution_assignments_node_id",
        "artifact_distribution_assignments",
        ["node_id"],
    )
    op.create_index(
        "ix_artifact_distribution_assignments_expires_at",
        "artifact_distribution_assignments",
        ["expires_at"],
    )
    op.create_index(
        "ix_artifact_distribution_assignments_state",
        "artifact_distribution_assignments",
        ["state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_artifact_distribution_assignments_state",
        table_name="artifact_distribution_assignments",
    )
    op.drop_index(
        "ix_artifact_distribution_assignments_expires_at",
        table_name="artifact_distribution_assignments",
    )
    op.drop_index(
        "ix_artifact_distribution_assignments_node_id",
        table_name="artifact_distribution_assignments",
    )
    op.drop_index(
        "ix_artifact_distribution_assignments_plan_digest",
        table_name="artifact_distribution_assignments",
    )
    op.drop_table("artifact_distribution_assignments")
