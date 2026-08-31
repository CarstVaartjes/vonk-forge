"""Fence exact distributed-rank observations across recovery launches."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_recipe_run_generation"
down_revision = "0010_managed_recipe_catalog_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("recipe_runs") as batch:
        batch.add_column(
            sa.Column(
                "run_generation",
                sa.BigInteger(),
                nullable=False,
                server_default="1",
            )
        )
        batch.create_check_constraint(
            "ck_recipe_runs_run_generation", "run_generation >= 1"
        )
    op.create_table(
        "recipe_run_observation_grants",
        sa.Column("run_node_id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("identity_sha256", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("consumed", sa.Boolean(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "length(identity_sha256) = 64 AND identity_sha256 = lower(identity_sha256)",
            name="ck_recipe_run_observation_grants_identity",
        ),
        sa.CheckConstraint(
            "expires_at >= issued_at",
            name="ck_recipe_run_observation_grants_expiry",
        ),
        sa.ForeignKeyConstraint(["run_node_id"], ["run_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_node_id"),
        sa.UniqueConstraint("request_id"),
    )


def downgrade() -> None:
    op.drop_table("recipe_run_observation_grants")
    with op.batch_alter_table("recipe_runs") as batch:
        batch.drop_constraint("ck_recipe_runs_run_generation", type_="check")
        batch.drop_column("run_generation")
