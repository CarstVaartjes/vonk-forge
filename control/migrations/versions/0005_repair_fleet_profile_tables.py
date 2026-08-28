"""Repair fleet profile tables omitted by the original baseline migration."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_repair_fleet_profile_tables"
down_revision = "0004_artifact_jobs"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        index["name"] == index_name for index in inspector.get_indexes(table_name)
    )


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if not _index_exists(table_name, name):
        op.create_index(name, table_name, columns, unique=False)


def upgrade() -> None:
    if not _table_exists("fleet_profiles"):
        op.create_table(
            "fleet_profiles",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column(
                "description",
                sa.String(length=1000),
                server_default="",
                nullable=False,
            ),
            sa.Column(
                "installation_policy",
                sa.String(length=24),
                server_default="keep-cached",
                nullable=False,
            ),
            sa.Column("assignments", sa.JSON(), nullable=False),
            sa.Column("labels", sa.JSON(), nullable=False),
            sa.Column("favorite", sa.Boolean(), server_default="0", nullable=False),
            sa.Column("created_by", sa.String(length=200), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "length(name) BETWEEN 1 AND 120",
                name="ck_fleet_profiles_name_length",
            ),
            sa.CheckConstraint(
                "length(description) <= 1000",
                name="ck_fleet_profiles_description_length",
            ),
            sa.CheckConstraint(
                "installation_policy IN ('keep-cached','exact')",
                name="ck_fleet_profiles_installation_policy",
            ),
            sa.CheckConstraint(
                "length(CAST(assignments AS TEXT)) BETWEEN 2 AND 131072",
                name="ck_fleet_profiles_assignments_size",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )

    _create_index_if_missing(
        "ix_fleet_profiles_created_at", "fleet_profiles", ["created_at"]
    )
    _create_index_if_missing(
        "ix_fleet_profiles_updated_at", "fleet_profiles", ["updated_at"]
    )

    if not _table_exists("fleet_profile_applications"):
        op.create_table(
            "fleet_profile_applications",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("request_key", sa.String(length=36), nullable=False),
            sa.Column("profile_id", sa.String(length=36), nullable=False),
            sa.Column("profile_digest", sa.String(length=64), nullable=False),
            sa.Column("plan_digest", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("plan", sa.JSON(), nullable=False),
            sa.Column("current_step", sa.Integer(), nullable=False),
            sa.Column("current_operation_id", sa.String(length=36), nullable=True),
            sa.Column("progress", sa.JSON(), nullable=False),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("status_reason", sa.String(length=512), nullable=True),
            sa.Column("actor", sa.String(length=200), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "state IN ('queued','running','waiting-for-operator','succeeded','failed','cancelled')",
                name="ck_fleet_profile_applications_state",
            ),
            sa.CheckConstraint(
                "length(profile_digest) = 64 AND profile_digest = lower(profile_digest) AND "
                "length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(profile_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0",
                name="ck_fleet_profile_applications_profile_digest",
            ),
            sa.CheckConstraint(
                "length(plan_digest) = 64 AND plan_digest = lower(plan_digest) AND "
                "length(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(replace(plan_digest, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), '5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), 'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '')) = 0",
                name="ck_fleet_profile_applications_plan_digest",
            ),
            sa.CheckConstraint(
                "current_step >= 0",
                name="ck_fleet_profile_applications_current_step",
            ),
            sa.CheckConstraint(
                "length(CAST(plan AS TEXT)) BETWEEN 2 AND 262144",
                name="ck_fleet_profile_applications_plan_size",
            ),
            sa.ForeignKeyConstraint(
                ["profile_id"], ["fleet_profiles.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("plan_digest"),
            sa.UniqueConstraint("request_key"),
        )

    _create_index_if_missing(
        "ix_fleet_profile_applications_created_at",
        "fleet_profile_applications",
        ["created_at"],
    )
    _create_index_if_missing(
        "ix_fleet_profile_applications_current_operation_id",
        "fleet_profile_applications",
        ["current_operation_id"],
    )
    _create_index_if_missing(
        "ix_fleet_profile_applications_profile_id",
        "fleet_profile_applications",
        ["profile_id"],
    )
    _create_index_if_missing(
        "ix_fleet_profile_applications_state",
        "fleet_profile_applications",
        ["state"],
    )


def downgrade() -> None:
    # This migration repairs objects that conceptually belong to the baseline.
    # Leave them for the baseline downgrade to remove, regardless of whether
    # this revision created them or found them already present.
    pass
