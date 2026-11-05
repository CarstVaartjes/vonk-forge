"""Take back the claims a failed profile load released, by admission's own rule.

The inverse of ``profile_capacity.release_unassigned_profile_claims``. Other
admissions may have used the released memory and disk meanwhile, so the claims
return only if the Spark still admits them. The decision is the one every
admission makes: the node's admission key serialises the check with the
insert, the demand is the load's own claims, every other reservation is
charged, and ``plan_capacity`` (the owner of the fit) decides. A shortfall
rolls the restore back and plans the load again like a fresh request
(``ConstraintReplan``), which then waits or evicts through the ordinary
review; nothing blocks.

The commitment half of the rule (pool - reserved - demand - floor) needs no
observation, so it is enforced even when the Spark's inventory is missing or
stale; only the observed-free half waits for a current sample.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import get_args

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ReservationState
from vonk_forge_contracts.memory_admission import evaluate_memory_admission

from . import fleet_profile_states
from .admission_locking import (
    AdmissionRowLock,
    ConstraintReplan,
    acquire_admission_keys,
    admitting,
    lock_admission_rows,
    node_admission_key,
)
from .disk_reservations import outstanding_disk_charges
from .memory_reservations import MEMORY_RESERVATION_KINDS, memory_reservations
from .models import FleetProfileApplication, ResourceReservation
from .profile_capacity import profile_memory_floor
from .resource_planning import (
    ResourceDemand,
    memory_capacity_snapshot,
    memory_reservation_kind,
    plan_capacity,
)
from .run_switch_contract import MemoryKind
from .run_switch_operations.planning_helpers import PlanInventory
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
    now: datetime,
    memory_floor_bytes: int,
    inventory_max_age_seconds: int,
    node_ids: Sequence[str] | None = None,
) -> None:
    """Take back the claims an application released while it was failed.

    Only rows that still carry the accepted plan return. A claim consumed by
    an installation or a run changed owner and is untouched. A port another
    load promised in the meantime cannot return (the unique promise index
    refuses it) and neither can memory or disk the Spark no longer admits:
    both raise ``ConstraintReplan``, so the load is planned again instead of
    resuming a plan whose claims it no longer holds.
    """
    if application.state not in fleet_profile_states.LIVE:
        return
    conditions = [
        ResourceReservation.owner_kind == "fleet-profile",
        ResourceReservation.owner_id == application.id,
        ResourceReservation.state == ReservationState.RELEASED,
        ResourceReservation.plan_digest == application.plan_digest,
    ]
    if node_ids is not None:
        conditions.append(ResourceReservation.node_id.in_(node_ids))
    # Admission order: the node keys first, then the rows.
    nodes = tuple(
        session.scalars(
            select(ResourceReservation.node_id)
            .where(*conditions)
            .distinct()
            .order_by(ResourceReservation.node_id)
        )
    )
    acquire_admission_keys(
        session,
        tuple(node_admission_key(node_id) for node_id in nodes),
        holder="profile-claim-restore",
    )
    restored: list[ResourceReservation] = []
    (locked,) = lock_admission_rows(
        session,
        (
            AdmissionRowLock(
                "claims",
                ResourceReservation,
                select(ResourceReservation).where(*conditions),
            ),
        ),
    ).values()
    for claim in locked:
        with admitting(), session.begin_nested():
            claim.state = (
                ReservationState.ACTIVE
                if claim.kind == "disk"
                else ReservationState.PROMISED
            )
            claim.released_at = None
            session.flush()
        restored.append(claim)
    inventory = PlanInventory(
        session, now=now, maximum_age_seconds=inventory_max_age_seconds
    )
    for node_id, kind in sorted(
        {(claim.node_id, claim.kind) for claim in restored if claim.kind != "port"}
    ):
        admitted = (
            _disk_admitted(session, inventory, node_id)
            if kind == "disk"
            else _memory_admitted(
                session, inventory, application, node_id, memory_floor_bytes
            )
        )
        if not admitted:
            raise ProfileClaimCapacityShort(node_id, kind)


def _disk_admitted(session: Session, inventory: PlanInventory, node_id: str) -> bool:
    snapshot, evidence = inventory.read(node_id)
    if snapshot is None:
        return True
    if snapshot.disk_total_bytes is None or snapshot.disk_free_bytes is None:
        return True
    committed = sum(
        charge.amount_bytes
        for charge in outstanding_disk_charges(
            session, node_id, inventory_observed_at=snapshot.observed_at
        )
    )
    current = (
        evidence.age_seconds is not None
        and evidence.maximum_age_seconds is not None
        and evidence.age_seconds <= evidence.maximum_age_seconds
    )
    if current:
        return (
            spark_free_bytes(snapshot.disk_free_bytes, snapshot.unused_model_bytes)
            - committed
            >= 0
        )
    # No current observation: the device size still bounds the commitments.
    return snapshot.disk_total_bytes - committed >= 0


def _memory_admitted(
    session: Session,
    inventory: PlanInventory,
    application: FleetProfileApplication,
    node_id: str,
    memory_floor_bytes: int,
) -> bool:
    """The load's own memory claims on the node, against everyone else's."""
    own = tuple(
        session.scalars(
            select(ResourceReservation)
            .where(
                ResourceReservation.owner_id == application.id,
                ResourceReservation.node_id == node_id,
                ResourceReservation.kind.in_(MEMORY_RESERVATION_KINDS),
                ResourceReservation.state.in_(
                    (ReservationState.ACTIVE, ReservationState.PROMISED)
                ),
            )
            .order_by(ResourceReservation.id)
        )
    )
    snapshot, evidence = inventory.read(node_id)
    if not own or snapshot is None:
        return True
    floor = max(
        (profile_memory_floor(session, claim) for claim in own),
        default=memory_floor_bytes,
    )
    demand = ResourceDemand(
        None,
        None,
        None,
        None,
        None,
        sum(claim.amount_bytes for claim in own),
        evidence.state,
    )
    kind = next(
        item
        for item in get_args(MemoryKind)
        if memory_reservation_kind(item) == own[0].kind
    )
    pool = snapshot.memory_pool
    capacity = memory_capacity_snapshot(
        node_id,
        kind,
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
            excluded_profile_application_ids=(application.id,),
            observed_at=snapshot.observed_at,
        ),
        memory_pool=pool,
        evidence_state=evidence.state,
    )
    current = (
        evidence.age_seconds is not None
        and evidence.maximum_age_seconds is not None
        and evidence.age_seconds <= evidence.maximum_age_seconds
    )
    if current:
        return plan_capacity(
            {node_id: demand}, [capacity], memory_floor_bytes=floor
        ).allowed
    # The pool is static: commitments are checked without the observed half.
    return all(
        part.available_bytes is None
        or evaluate_memory_admission(
            demand.total_bytes or 0,
            part.available_bytes,
            memory_floor_bytes=floor,
            reserved_bytes=part.reserved_bytes or 0,
        ).commitment_headroom_bytes
        >= 0
        for part in (capacity.components or (capacity,))
    )
