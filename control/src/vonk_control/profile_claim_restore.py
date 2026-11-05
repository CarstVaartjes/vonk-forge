"""Take back the claims a failed profile load released, within real capacity.

The inverse of ``profile_capacity.release_unassigned_profile_claims``. Other
admissions may have used the released memory and disk meanwhile, so the claims
return only if the Spark still fits them: the same ledger projections the
ordinary admission reads. A shortfall rolls the restore back and plans the
load again like a fresh request (``ConstraintReplan``), which then waits or
evicts through the ordinary review; nothing blocks.
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ReservationState

from . import fleet_profile_states
from .admission_locking import ConstraintReplan, admitting
from .disk_reservations import outstanding_disk_charges
from .memory_reservations import memory_reservations
from .models import FleetProfileApplication, NodeInventorySnapshot, ResourceReservation
from .resource_planning import CapacitySnapshot, memory_capacity_snapshot
from .spark_disk import spark_free_bytes

#: The restored claims no longer fit the Spark: plan the load again.
CAPACITY_SHORT_CONSTRAINT = "profile_claim_capacity"


class ProfileClaimCapacityShort(ConstraintReplan):
    """Other work took the capacity a released claim held: plan again."""

    def __init__(self, node_id: str, kind: str) -> None:
        super().__init__(
            f"Spark {node_id} no longer has the {kind} this load released; "
            "the load is planned again.",
            constraint=CAPACITY_SHORT_CONSTRAINT,
        )


def restore_released_profile_claims(
    session: Session,
    application: FleetProfileApplication,
    *,
    node_ids: Sequence[str] | None = None,
) -> None:
    """Take back the claims an application released while it was failed.

    Only rows that still carry the accepted plan return. A claim consumed by
    an installation or a run changed owner and is untouched. A port another
    load promised in the meantime cannot return (the unique promise index
    refuses it) and neither can memory or disk the Spark no longer has: both
    raise ``ConstraintReplan``, so the load is planned again instead of
    resuming a plan whose claims it no longer holds.
    """
    if application.state not in fleet_profile_states.LIVE:
        return
    claims = select(ResourceReservation).where(
        ResourceReservation.owner_kind == "fleet-profile",
        ResourceReservation.owner_id == application.id,
        ResourceReservation.state == ReservationState.RELEASED,
        ResourceReservation.plan_digest == application.plan_digest,
    )
    if node_ids is not None:
        claims = claims.where(ResourceReservation.node_id.in_(node_ids))
    restored: list[ResourceReservation] = []
    for claim in session.scalars(
        claims.order_by(ResourceReservation.id).with_for_update(nowait=True)
    ):
        with admitting(), session.begin_nested():
            claim.state = (
                ReservationState.ACTIVE
                if claim.kind == "disk"
                else ReservationState.PROMISED
            )
            claim.released_at = None
            session.flush()
        restored.append(claim)
    for node_id, kind in sorted(
        {(claim.node_id, claim.kind) for claim in restored if claim.kind != "port"}
    ):
        if not _fits(session, node_id, kind):
            raise ProfileClaimCapacityShort(node_id, kind)


def _fits(session: Session, node_id: str, kind: str) -> bool:
    """Whether the ledger, restored claims included, stays within the Spark."""
    snapshot = session.scalar(
        select(NodeInventorySnapshot)
        .where(NodeInventorySnapshot.node_id == node_id)
        .order_by(NodeInventorySnapshot.observed_at.desc())
        .limit(1)
    )
    if snapshot is None:
        return False
    if kind == "disk":
        if snapshot.disk_free_bytes is None:
            return False
        committed = sum(
            charge.amount_bytes
            for charge in outstanding_disk_charges(
                session, node_id, inventory_observed_at=snapshot.observed_at
            )
        )
        return (
            spark_free_bytes(snapshot.disk_free_bytes, snapshot.unused_model_bytes)
            - committed
            >= 0
        )
    pool = snapshot.memory_pool
    capacity = memory_capacity_snapshot(
        node_id,
        "unified",
        host=(snapshot.host_memory_total_bytes, snapshot.host_memory_free_bytes)
        if snapshot.host_memory_total_bytes is not None
        and snapshot.host_memory_free_bytes is not None
        else None,
        accelerator=(snapshot.gpu_memory_total_bytes, snapshot.gpu_memory_free_bytes)
        if snapshot.gpu_memory_total_bytes is not None
        and snapshot.gpu_memory_free_bytes is not None
        else None,
        reservations=memory_reservations(
            session,
            node_id,
            memory_pool=pool,
            observed_at=snapshot.observed_at,
        ),
        memory_pool=pool,
        evidence_state="fresh",
    )
    return all(_headroom(part) >= 0 for part in (capacity.components or (capacity,)))


def _headroom(part: CapacitySnapshot) -> int:
    """What the Spark has left after the claims and what it already holds."""
    if (
        part.available_bytes is None
        or part.occupied_bytes is None
        or part.reserved_bytes is None
    ):
        return -1
    return min(
        part.available_bytes - part.reserved_bytes,
        part.available_bytes - part.occupied_bytes - (part.unmaterialized_bytes or 0),
    )
