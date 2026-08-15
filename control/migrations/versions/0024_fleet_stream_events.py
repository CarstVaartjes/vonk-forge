"""Add the serialized durable Fleet event outbox."""

import sqlalchemy as sa
from alembic import op

revision = "0024_fleet_stream_events"
down_revision = "0023_node_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    payload_byte_length = (
        "length(CAST(payload AS BLOB))"
        if op.get_bind().dialect.name == "sqlite"
        else "octet_length(CAST(payload AS TEXT))"
    )
    cursor = op.create_table(
        "fleet_event_cursor",
        sa.Column("singleton_id", sa.SmallInteger, primary_key=True),
        sa.Column("last_id", sa.BigInteger, nullable=False),
        sa.CheckConstraint(
            "singleton_id = 1", name="ck_fleet_event_cursor_singleton"
        ),
        sa.CheckConstraint("last_id >= 0", name="ck_fleet_event_cursor_last_id"),
    )
    op.bulk_insert(cursor, [{"singleton_id": 1, "last_id": 0}])
    op.create_table(
        "fleet_stream_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("node_id", sa.String(36)),
        sa.Column("entity_kind", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('node-telemetry','recipe-state','operation-state')",
            name="ck_fleet_stream_events_event_type",
        ),
        sa.CheckConstraint(
            "expires_at > occurred_at", name="ck_fleet_stream_events_expiry"
        ),
        sa.CheckConstraint(
            f"{payload_byte_length} BETWEEN 2 AND 8192",
            name="ck_fleet_stream_events_payload_size",
        ),
    )
    op.create_index(
        "ix_fleet_stream_events_expires_id",
        "fleet_stream_events",
        ["expires_at", "id"],
    )
    op.create_index(
        "ix_fleet_stream_events_node_id",
        "fleet_stream_events",
        ["node_id", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fleet_stream_events_node_id", table_name="fleet_stream_events"
    )
    op.drop_index(
        "ix_fleet_stream_events_expires_id", table_name="fleet_stream_events"
    )
    op.drop_table("fleet_stream_events")
    op.drop_table("fleet_event_cursor")
