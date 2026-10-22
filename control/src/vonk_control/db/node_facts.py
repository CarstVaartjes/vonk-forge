"""One-time transactional adoption of retired per-node fact owners."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection


def move_contact_facts(connection: Connection) -> None:
    """Copy before dropping; a crash rolls back both effects for startup retry."""
    if "agent_presence" not in inspect(connection).get_table_names():
        return
    connection.execute(
        text("""
        UPDATE agent_nodes AS node
        SET management_address = presence.management_address,
            management_observed_at = presence.observed_at,
            contact_certificate_serial = presence.certificate_serial
        FROM agent_presence AS presence
        WHERE node.node_id = presence.node_id
          AND (node.management_observed_at IS NULL
               OR presence.observed_at > node.management_observed_at)
    """)
    )
    connection.execute(text("DROP TABLE agent_presence"))
