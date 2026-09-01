"""Fence exact distributed-rank observations across recovery launches."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_recipe_run_generation"
down_revision = "0011_recipe_model_identity"
branch_labels = None
depends_on = None


def _nullable_lower_hex(column: str, length: int) -> str:
    remainder = column
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return (
        f"{column} IS NULL OR (length({column}) = {length} AND "
        f"{column} = lower({column}) AND length({remainder}) = 0)"
    )


def upgrade() -> None:
    with op.batch_alter_table("agent_enrollments") as batch:
        batch.add_column(
            sa.Column(
                "observation_receipt_public_key", sa.String(length=64), nullable=True
            )
        )
        batch.create_check_constraint(
            "ck_agent_enrollments_observation_receipt_public_key",
            _nullable_lower_hex("observation_receipt_public_key", 64),
        )
    with op.batch_alter_table("agent_nodes") as batch:
        batch.add_column(
            sa.Column(
                "observation_receipt_public_key", sa.String(length=64), nullable=True
            )
        )
        batch.create_check_constraint(
            "ck_agent_nodes_observation_receipt_public_key",
            _nullable_lower_hex("observation_receipt_public_key", 64),
        )
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
        batch.add_column(
            sa.Column("observation_deadline_at", sa.DateTime(timezone=True))
        )
    with op.batch_alter_table("run_nodes") as batch:
        batch.add_column(
            sa.Column("observed_run_generation", sa.BigInteger(), nullable=True)
        )
        batch.add_column(
            sa.Column("observation_receipt_sha256", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("observation_endpoint_ready", sa.Boolean(), nullable=True)
        )
        batch.create_check_constraint(
            "ck_run_nodes_observed_run_generation",
            "observed_run_generation IS NULL OR observed_run_generation >= 1",
        )
        batch.create_check_constraint(
            "ck_run_nodes_observation_receipt",
            _nullable_lower_hex("observation_receipt_sha256", 64),
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
    with op.batch_alter_table("run_nodes") as batch:
        batch.drop_constraint("ck_run_nodes_observation_receipt", type_="check")
        batch.drop_constraint("ck_run_nodes_observed_run_generation", type_="check")
        batch.drop_column("observation_endpoint_ready")
        batch.drop_column("observation_receipt_sha256")
        batch.drop_column("observed_run_generation")
    with op.batch_alter_table("recipe_runs") as batch:
        batch.drop_constraint("ck_recipe_runs_run_generation", type_="check")
        batch.drop_column("observation_deadline_at")
        batch.drop_column("run_generation")
    with op.batch_alter_table("agent_nodes") as batch:
        batch.drop_constraint(
            "ck_agent_nodes_observation_receipt_public_key", type_="check"
        )
        batch.drop_column("observation_receipt_public_key")
    with op.batch_alter_table("agent_enrollments") as batch:
        batch.drop_constraint(
            "ck_agent_enrollments_observation_receipt_public_key", type_="check"
        )
        batch.drop_column("observation_receipt_public_key")
