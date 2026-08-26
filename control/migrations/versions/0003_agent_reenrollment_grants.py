"""Allow explicit one-time Spark re-enrollment grants."""

from __future__ import annotations

from alembic import op

revision = "0003_agent_reenrollment_grants"
down_revision = "0002_fleet_node_profile_events"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_agent_enrollment_grants_purpose"
_PREVIOUS = "purpose = 'new-node'"
_CURRENT = "purpose IN ('new-node', 're-enroll')"


def _replace(expression: str) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table(
            "agent_enrollment_grants", recreate="always"
        ) as batch:
            batch.drop_constraint(_CONSTRAINT, type_="check")
            batch.create_check_constraint(_CONSTRAINT, expression)
        return
    op.drop_constraint(_CONSTRAINT, "agent_enrollment_grants", type_="check")
    op.create_check_constraint(_CONSTRAINT, "agent_enrollment_grants", expression)


def upgrade() -> None:
    _replace(_CURRENT)


def downgrade() -> None:
    _replace(_PREVIOUS)
