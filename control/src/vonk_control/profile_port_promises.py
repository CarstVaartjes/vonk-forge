"""Which endpoint port promises still bind a Spark port, and to whom.

A profile load promises each serving assignment its host ports when it is
admitted, and that assignment's run consumes exactly its own promise. A promise
binds only while its owner can still consume it: an owner that ended, vanished
or was fenced by a newer workload intent leaves a promise nothing will ever
take. Such a promise never blocks another load; it is released where a new
promise for the same port meets it.

Assignments of one load that share a Spark are given distinct ports: the
review and each run count the ports the load gives its other assignments as
taken.
"""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import or_, select, tuple_
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import (
    LifecycleState,
    ReservationState,
    canonical_message,
)

from . import job_states
from .admission_locking import ConstraintReplan
from .fleet_profile_contract import FleetProfileApplicationProgress
from .models import AgentNode, FleetProfileApplication, ResourceReservation
from .strict_json import read_stored_model

#: The unique index behind "one live promise per Spark port".
PORT_PROMISE_CONSTRAINT = "uq_promised_node_port"

_LIVE = job_states.words(
    LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
)


class PortPromiseTaken(ConstraintReplan):
    """A live load already holds the port this load's plan promises: plan again.

    The plan's own check of the conflict the promise index decides (REPLAN).
    """

    def __init__(self, node_id: str, port: int) -> None:
        super().__init__(
            f"Port {port} on Spark {node_id} is promised to another workload; "
            "the load is planned again with a free port.",
            constraint=PORT_PROMISE_CONSTRAINT,
        )


def port_binds() -> ColumnElement[bool]:
    """Active bindings, and promises whose owning load is still live."""
    return or_(
        ResourceReservation.state != ReservationState.PROMISED,
        ResourceReservation.owner_id.in_(
            select(FleetProfileApplication.id).where(
                FleetProfileApplication.state.in_(_LIVE)
            )
        ),
    )


def clear_port_promises(
    session: Session,
    application_id: str,
    wanted: Iterable[tuple[str, int]],
    *,
    now: datetime,
) -> None:
    """Make room for this load's promises, or refuse with the live owner.

    A promise nothing can consume any more is released here (self-healing).
    A port promised twice by this load's own plan, or held by a live load
    that can still consume it, raises ``PortPromiseTaken`` so admission plans
    again instead of writing a second promise for one port.
    """
    pairs = list(wanted)
    seen: set[tuple[str, int]] = set()
    for pair in pairs:
        if pair in seen:
            raise PortPromiseTaken(*pair)
        seen.add(pair)
    if not pairs:
        return
    held = session.scalars(
        select(ResourceReservation)
        .where(
            ResourceReservation.kind == "port",
            ResourceReservation.state == ReservationState.PROMISED,
            ResourceReservation.owner_id != application_id,
            tuple_(ResourceReservation.node_id, ResourceReservation.resource_key).in_(
                [(node_id, str(port)) for node_id, port in pairs]
            ),
        )
        .order_by(ResourceReservation.node_id, ResourceReservation.resource_key)
        .with_for_update(nowait=True)
    )
    for claim in held:
        if _consumable(session, claim):
            raise PortPromiseTaken(claim.node_id, int(claim.resource_key))
        claim.state = ReservationState.RELEASED
        claim.released_at = now
    session.flush()


def _consumable(session: Session, claim: ResourceReservation) -> bool:
    """Whether the promise's owner can still hand it to a run."""
    owner = session.get(FleetProfileApplication, claim.owner_id)
    node = session.get(AgentNode, claim.node_id)
    if owner is None or node is None or owner.state not in _LIVE:
        return False
    try:
        progress = read_stored_model(
            FleetProfileApplicationProgress,
            canonical_message(owner.progress),
            strict=True,
            from_json=True,
        )
    except (TypeError, ValueError):
        return False
    return progress.workload_intent_ordinal == node.workload_intent_ordinal
