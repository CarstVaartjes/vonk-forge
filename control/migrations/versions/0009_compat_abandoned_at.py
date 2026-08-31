"""Retain compatibility-recovery abandonment evidence after reauthorization."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_compat_abandoned_at"
down_revision = "0008_compat_recovery_abandon"
branch_labels = None
depends_on = None

_TABLE = "agent_upgrade_compatibility_recoveries"
_FIELDS = "ck_agent_upgrade_compatibility_recoveries_state_fields"
_OLD_FIELDS_EXPRESSION = (
    "((state = 'armed' AND issued_at IS NULL AND completed_at IS NULL "
    "AND blocked_at IS NULL) OR "
    "(state IN ('issued','awaiting-identity') AND issued_at IS NOT NULL "
    "AND completed_at IS NULL AND blocked_at IS NULL) OR "
    "(state = 'completed' AND issued_at IS NOT NULL "
    "AND completed_at IS NOT NULL) OR "
    "(state = 'completed-before-dispatch' AND issued_at IS NULL "
    "AND completed_at IS NOT NULL) OR "
    "(state = 'operator-blocked' AND blocked_at IS NOT NULL "
    "AND completed_at IS NULL) OR "
    "(state = 'abandoned' AND blocked_at IS NOT NULL "
    "AND completed_at IS NOT NULL))"
)
_NEW_FIELDS_EXPRESSION = _OLD_FIELDS_EXPRESSION.replace(
    "AND completed_at IS NOT NULL))",
    "AND completed_at IS NOT NULL AND abandoned_at IS NOT NULL))",
)


def _replace_fields_constraint(expression: str) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.drop_constraint(_FIELDS, type_="check")
            batch.create_check_constraint(_FIELDS, expression)
        return
    op.drop_constraint(_FIELDS, _TABLE, type_="check")
    op.create_check_constraint(_FIELDS, _TABLE, expression)


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        f"UPDATE {_TABLE} SET abandoned_at = completed_at WHERE state = 'abandoned'"
    )
    _replace_fields_constraint(_NEW_FIELDS_EXPRESSION)


def downgrade() -> None:
    _replace_fields_constraint(_OLD_FIELDS_EXPRESSION)
    op.drop_column(_TABLE, "abandoned_at")
