"""The one writer of Controller-observed contact facts (``agent_node_contacts``).

An authenticated agent request records its contact with a plain upsert of the
node's contact row. It never selects ``agent_nodes`` FOR UPDATE and never
updates that row, so contact traffic cannot hold the identity/intent row that
admission and publication lock. Only contact writers touch the contact row,
and each holds it for one statement at the end of its transaction.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ColumnElement, or_
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session
from sqlalchemy.orm.util import identity_key

from .models import AgentNode, AgentNodeContact


def _insert(session: Session) -> postgresql.Insert | sqlite.Insert:
    if session.get_bind().dialect.name == "postgresql":
        return postgresql.insert(AgentNodeContact)
    return sqlite.insert(AgentNodeContact)


def _forget_loaded(session: Session, node_id: str) -> None:
    """A Core upsert bypasses the identity map; reload on next access."""
    contact = session.identity_map.get(identity_key(AgentNodeContact, node_id))
    if contact is not None:
        session.expire(contact)
    node = session.identity_map.get(identity_key(AgentNode, node_id))
    if node is not None:
        session.expire(node, ["contact"])


def record_contact(
    session: Session,
    node_id: str,
    *,
    last_seen_at: datetime | None = None,
    protocol_version: int | None = None,
    contact_certificate_serial: str | None = None,
    contact_observation_digest: str | None = None,
    management_address: str | None = None,
    management_observed_at: datetime | None = None,
) -> None:
    """Upsert the given facts; a fact left out keeps its stored value.

    The newest accepted contact replaces the stored stamp, even after a
    Controller clock rollback: contact time is the Controller's own clock.
    """
    facts = {
        column: value
        for column, value in (
            (AgentNodeContact.last_seen_at, last_seen_at),
            (AgentNodeContact.protocol_version, protocol_version),
            (AgentNodeContact.contact_certificate_serial, contact_certificate_serial),
            (AgentNodeContact.contact_observation_digest, contact_observation_digest),
            (AgentNodeContact.management_address, management_address),
            (AgentNodeContact.management_observed_at, management_observed_at),
        )
        if value is not None
    }
    if not facts:
        return
    insert = _insert(session).values({AgentNodeContact.node_id: node_id, **facts})
    session.execute(
        insert.on_conflict_do_update(
            index_elements=[AgentNodeContact.node_id],
            set_={column.key: insert.excluded[column.key] for column in facts},
        )
    )
    _forget_loaded(session, node_id)


def record_observation_delivery(
    session: Session, node_id: str, status: int, started: datetime
) -> None:
    """Keep the newest delivery verdict; a delayed response never overwrites it."""
    insert = _insert(session).values(
        node_id=node_id,
        recipe_observation_http_status=status,
        recipe_observation_status_at=started,
    )
    newer: ColumnElement[bool] = or_(
        AgentNodeContact.recipe_observation_status_at.is_(None),
        AgentNodeContact.recipe_observation_status_at
        <= insert.excluded.recipe_observation_status_at,
    )
    session.execute(
        insert.on_conflict_do_update(
            index_elements=[AgentNodeContact.node_id],
            set_={
                "recipe_observation_http_status": (
                    insert.excluded.recipe_observation_http_status
                ),
                "recipe_observation_status_at": (
                    insert.excluded.recipe_observation_status_at
                ),
            },
            where=newer,
        )
    )
    _forget_loaded(session, node_id)


def lock_contact(session: Session, node_id: str) -> AgentNodeContact | None:
    """Return the node's contact row for a read-modify-write of its evidence.

    Creates the row if absent and locks only that row: contact writers of the
    same Spark serialize on it, admission and publication never touch it.
    An unknown Spark has no contact row.
    """
    if session.get(AgentNode, node_id) is None:
        return None
    insert = _insert(session).values(node_id=node_id)
    session.execute(
        insert.on_conflict_do_nothing(index_elements=[AgentNodeContact.node_id])
    )
    _forget_loaded(session, node_id)
    return session.get(
        AgentNodeContact, node_id, with_for_update=True, populate_existing=True
    )


def last_seen(node: AgentNode) -> datetime | None:
    """The node's latest accepted contact on the Controller clock, if any."""
    return None if node.contact is None else node.contact.last_seen_at
