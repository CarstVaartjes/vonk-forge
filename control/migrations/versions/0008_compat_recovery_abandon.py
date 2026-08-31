"""Allow a timed-out compatibility recovery to be abandoned safely."""

from __future__ import annotations

from alembic import op

revision = "0008_compat_recovery_abandon"
down_revision = "0007_compat_rearm_certificates"
branch_labels = None
depends_on = None

_TABLE = "agent_upgrade_compatibility_recoveries"
_STATE = "ck_agent_upgrade_compatibility_recoveries_state"
_FIELDS = "ck_agent_upgrade_compatibility_recoveries_state_fields"
_OLD_STATE_EXPRESSION = (
    "state IN ('armed','issued','awaiting-identity','completed',"
    "'completed-before-dispatch','operator-blocked')"
)
_NEW_STATE_EXPRESSION = (
    "state IN ('armed','issued','awaiting-identity','completed',"
    "'completed-before-dispatch','operator-blocked','abandoned')"
)
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
    "AND completed_at IS NULL))"
)
_NEW_FIELDS_EXPRESSION = (
    _OLD_FIELDS_EXPRESSION[:-1] + " OR (state = 'abandoned' AND blocked_at IS NOT NULL "
    "AND completed_at IS NOT NULL))"
)


def _replace_constraints(*, state_expression: str, fields_expression: str) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            batch.drop_constraint(_FIELDS, type_="check")
            batch.drop_constraint(_STATE, type_="check")
            batch.create_check_constraint(_STATE, state_expression)
            batch.create_check_constraint(_FIELDS, fields_expression)
        return
    op.drop_constraint(_FIELDS, _TABLE, type_="check")
    op.drop_constraint(_STATE, _TABLE, type_="check")
    op.create_check_constraint(_STATE, _TABLE, state_expression)
    op.create_check_constraint(_FIELDS, _TABLE, fields_expression)


def upgrade() -> None:
    _replace_constraints(
        state_expression=_NEW_STATE_EXPRESSION,
        fields_expression=_NEW_FIELDS_EXPRESSION,
    )


def downgrade() -> None:
    # Older schemas cannot represent the terminal abandoned state. Preserve the
    # fail-closed quarantine when downgrading rather than deleting audit rows.
    op.execute(
        f"UPDATE {_TABLE} SET state = 'operator-blocked', completed_at = NULL "
        "WHERE state = 'abandoned'"
    )
    _replace_constraints(
        state_expression=_OLD_STATE_EXPRESSION,
        fields_expression=_OLD_FIELDS_EXPRESSION,
    )
