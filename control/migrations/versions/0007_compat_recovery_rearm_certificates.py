"""Persist the exact certificate pair approved for compatibility re-arm."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_compat_recovery_rearm_certificates"
down_revision = "0006_spark3542_compat_recovery"
branch_labels = None
depends_on = None

_TABLE = "agent_upgrade_compatibility_recoveries"
_PAIRED_CONSTRAINT = "ck_agent_upgrade_compatibility_recoveries_rearm_certs_paired"
_MATCH_CONSTRAINT = "ck_agent_upgrade_compatibility_recoveries_retry_matches_rearm"
_PAIRED_EXPRESSION = (
    "((rearm_attempt_certificate_serial IS NULL "
    "AND rearm_dispatch_certificate_serial IS NULL) OR "
    "(rearm_attempt_certificate_serial IS NOT NULL "
    "AND rearm_dispatch_certificate_serial IS NOT NULL))"
)
_MATCH_EXPRESSION = (
    "(rearm_dispatch_certificate_serial IS NULL "
    "OR retry_certificate_serial IS NULL "
    "OR retry_certificate_serial = rearm_dispatch_certificate_serial)"
)


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.add_column(
                sa.Column(
                    "rearm_attempt_certificate_serial",
                    sa.String(length=128),
                    nullable=True,
                )
            )
            batch.add_column(
                sa.Column(
                    "rearm_dispatch_certificate_serial",
                    sa.String(length=128),
                    nullable=True,
                )
            )
            batch.create_check_constraint(_PAIRED_CONSTRAINT, _PAIRED_EXPRESSION)
            batch.create_check_constraint(_MATCH_CONSTRAINT, _MATCH_EXPRESSION)
        return
    op.add_column(
        _TABLE,
        sa.Column(
            "rearm_attempt_certificate_serial", sa.String(length=128), nullable=True
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "rearm_dispatch_certificate_serial", sa.String(length=128), nullable=True
        ),
    )
    op.create_check_constraint(_PAIRED_CONSTRAINT, _TABLE, _PAIRED_EXPRESSION)
    op.create_check_constraint(_MATCH_CONSTRAINT, _TABLE, _MATCH_EXPRESSION)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.drop_constraint(_MATCH_CONSTRAINT, type_="check")
            batch.drop_constraint(_PAIRED_CONSTRAINT, type_="check")
            batch.drop_column("rearm_dispatch_certificate_serial")
            batch.drop_column("rearm_attempt_certificate_serial")
        return
    op.drop_constraint(_MATCH_CONSTRAINT, _TABLE, type_="check")
    op.drop_constraint(_PAIRED_CONSTRAINT, _TABLE, type_="check")
    op.drop_column(_TABLE, "rearm_dispatch_certificate_serial")
    op.drop_column(_TABLE, "rearm_attempt_certificate_serial")
