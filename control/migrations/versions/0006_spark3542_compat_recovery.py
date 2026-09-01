"""Persist the one-shot Spark3542 staged-recovery reboot grant."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_spark3542_compat_recovery"
down_revision = "0005_repair_fleet_profile_tables"
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
        "agent_upgrade_compatibility_recoveries",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("source_attempt", sa.Integer(), nullable=False),
        sa.Column("source_fence", sa.String(length=36), nullable=False),
        sa.Column("source_certificate_serial", sa.String(length=128), nullable=False),
        sa.Column("expected_retry_attempt", sa.Integer(), nullable=False),
        sa.Column("retry_fence", sa.String(length=36), nullable=True),
        sa.Column("retry_certificate_serial", sa.String(length=128), nullable=True),
        sa.Column("source_semantic_version", sa.String(length=32), nullable=False),
        sa.Column("source_build_digest", sa.String(length=71), nullable=False),
        sa.Column("source_binary_digest", sa.String(length=64), nullable=False),
        sa.Column("upgrade_payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("package_sha256", sa.String(length=64), nullable=False),
        sa.Column("target_package_version", sa.String(length=128), nullable=False),
        sa.Column("target_build_digest", sa.String(length=71), nullable=False),
        sa.Column("target_binary_digest", sa.String(length=64), nullable=False),
        sa.Column("authority_revision", sa.String(length=128), nullable=False),
        sa.Column("plan_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("signed_grant", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("grant_request_id", sa.String(length=36), nullable=True),
        sa.Column("grant_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("identity_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('armed','issued','awaiting-identity','completed',"
            "'completed-before-dispatch','operator-blocked')",
            name="ck_agent_upgrade_compatibility_recoveries_state",
        ),
        sa.CheckConstraint(
            "((retry_fence IS NULL AND retry_certificate_serial IS NULL "
            "AND signed_grant IS NULL AND grant_request_id IS NULL "
            "AND grant_expires_at IS NULL AND identity_deadline IS NULL "
            "AND issued_at IS NULL) OR "
            "(retry_fence IS NOT NULL AND retry_certificate_serial IS NOT NULL "
            "AND signed_grant IS NOT NULL AND grant_request_id IS NOT NULL "
            "AND grant_expires_at IS NOT NULL AND identity_deadline IS NOT NULL "
            "AND issued_at IS NOT NULL))",
            name="ck_agent_upgrade_compatibility_recoveries_grant_all_or_none",
        ),
        sa.CheckConstraint(
            "((state = 'armed' AND issued_at IS NULL AND completed_at IS NULL "
            "AND blocked_at IS NULL) OR "
            "(state IN ('issued','awaiting-identity') AND issued_at IS NOT NULL "
            "AND completed_at IS NULL AND blocked_at IS NULL) OR "
            "(state = 'completed' AND issued_at IS NOT NULL "
            "AND completed_at IS NOT NULL) OR "
            "(state = 'completed-before-dispatch' AND issued_at IS NULL "
            "AND completed_at IS NOT NULL) OR "
            "(state = 'operator-blocked' AND blocked_at IS NOT NULL "
            "AND completed_at IS NULL))",
            name="ck_agent_upgrade_compatibility_recoveries_state_fields",
        ),
        sa.CheckConstraint(
            "expected_retry_attempt = source_attempt + 1",
            name="ck_agent_upgrade_compatibility_recoveries_attempt_sequence",
        ),
        sa.CheckConstraint(
            _lower_hex("plan_digest", 64),
            name="ck_agent_upgrade_compatibility_recoveries_plan_digest",
        ),
        sa.CheckConstraint(
            _lower_hex("source_binary_digest", 64),
            name="ck_agent_upgrade_compatibility_recoveries_source_binary",
        ),
        sa.CheckConstraint(
            _lower_hex("package_sha256", 64),
            name="ck_agent_upgrade_compatibility_recoveries_package",
        ),
        sa.CheckConstraint(
            _lower_hex("upgrade_payload_sha256", 64),
            name="ck_agent_upgrade_compatibility_recoveries_upgrade_payload",
        ),
        sa.CheckConstraint(
            _lower_hex("target_binary_digest", 64),
            name="ck_agent_upgrade_compatibility_recoveries_target_binary",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["node_id"], ["agent_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["agent_operations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("grant_request_id"),
        sa.UniqueConstraint("operation_id"),
        sa.UniqueConstraint("plan_digest"),
        sa.UniqueConstraint("request_id"),
        sa.UniqueConstraint("retry_fence"),
    )
    op.create_index(
        "ix_agent_upgrade_compatibility_recoveries_job_id",
        "agent_upgrade_compatibility_recoveries",
        ["job_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_upgrade_compatibility_recoveries_node_id",
        "agent_upgrade_compatibility_recoveries",
        ["node_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_upgrade_compatibility_recoveries_node_id",
        table_name="agent_upgrade_compatibility_recoveries",
    )
    op.drop_index(
        "ix_agent_upgrade_compatibility_recoveries_job_id",
        table_name="agent_upgrade_compatibility_recoveries",
    )
    op.drop_table("agent_upgrade_compatibility_recoveries")
