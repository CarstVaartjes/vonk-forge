"""Add durable managed recipe-library synchronization authority."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_managed_recipe_catalog_sync"
down_revision = "0009_compat_abandoned_at"
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


def _nullable_lower_hex(column: str, length: int) -> str:
    return f"{column} IS NULL OR ({_lower_hex(column, length)})"


def upgrade() -> None:
    op.create_table(
        "recipe_library_sync_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_key", sa.String(length=36), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("active_slot", sa.String(length=32), nullable=True),
        sa.Column("repository", sa.String(length=200), nullable=False),
        sa.Column("expected_commit", sa.String(length=40), nullable=True),
        sa.Column("observed_commit", sa.String(length=40), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("processed_count", sa.Integer(), nullable=False),
        sa.Column("imported_count", sa.Integer(), nullable=False),
        sa.Column("updated_count", sa.Integer(), nullable=False),
        sa.Column("current_count", sa.Integer(), nullable=False),
        sa.Column("conflict_count", sa.Integer(), nullable=False),
        sa.Column("missing_count", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("error_detail", sa.String(length=256), nullable=True),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('manual','automatic')",
            name="ck_recipe_library_sync_runs_trigger",
        ),
        sa.CheckConstraint(
            "state IN ('running','succeeded','failed')",
            name="ck_recipe_library_sync_runs_state",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("expected_commit", 40),
            name="ck_recipe_library_sync_runs_expected_commit",
        ),
        sa.CheckConstraint(
            _nullable_lower_hex("observed_commit", 40),
            name="ck_recipe_library_sync_runs_observed_commit",
        ),
        sa.CheckConstraint(
            "total_count >= 0 AND processed_count >= 0 AND imported_count >= 0 "
            "AND updated_count >= 0 AND current_count >= 0 "
            "AND conflict_count >= 0 AND missing_count >= 0 "
            "AND processed_count <= total_count",
            name="ck_recipe_library_sync_runs_counts",
        ),
        sa.CheckConstraint(
            "(state = 'running' AND completed_at IS NULL) OR "
            "(state IN ('succeeded','failed') AND completed_at IS NOT NULL)",
            name="ck_recipe_library_sync_runs_completion",
        ),
        sa.CheckConstraint(
            "(state = 'running' AND active_slot = 'managed-recipes') OR "
            "(state IN ('succeeded','failed') AND active_slot IS NULL)",
            name="ck_recipe_library_sync_runs_active_slot",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_key"),
        sa.UniqueConstraint("active_slot"),
    )
    op.create_index(
        op.f("ix_recipe_library_sync_runs_trigger"),
        "recipe_library_sync_runs",
        ["trigger"],
        unique=False,
    )
    op.create_index(
        op.f("ix_recipe_library_sync_runs_state"),
        "recipe_library_sync_runs",
        ["state"],
        unique=False,
    )
    op.create_index(
        op.f("ix_recipe_library_sync_runs_observed_commit"),
        "recipe_library_sync_runs",
        ["observed_commit"],
        unique=False,
    )
    op.create_index(
        op.f("ix_recipe_library_sync_runs_created_at"),
        "recipe_library_sync_runs",
        ["created_at"],
        unique=False,
    )
def downgrade() -> None:
    op.drop_table("recipe_library_sync_runs")
