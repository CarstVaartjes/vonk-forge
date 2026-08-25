"""Allow retained Fleet events for mutable node profiles."""

from __future__ import annotations

from alembic import op

revision = "0002_fleet_node_profile_events"
down_revision = "0001_fleet_library_baseline"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_fleet_stream_events_event_type"
_PREVIOUS_TYPES = "event_type IN ('node-telemetry','recipe-state','operation-state')"
_CURRENT_TYPES = (
    "event_type IN ('node-telemetry','node-profile','recipe-state','operation-state')"
)


def _replace_event_type_constraint(expression: str) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        with op.batch_alter_table("fleet_stream_events", recreate="always") as batch:
            batch.drop_constraint(_CONSTRAINT, type_="check")
            batch.create_check_constraint(_CONSTRAINT, expression)
        return
    op.drop_constraint(_CONSTRAINT, "fleet_stream_events", type_="check")
    op.create_check_constraint(_CONSTRAINT, "fleet_stream_events", expression)


def upgrade() -> None:
    _replace_event_type_constraint(_CURRENT_TYPES)


def downgrade() -> None:
    _replace_event_type_constraint(_PREVIOUS_TYPES)
