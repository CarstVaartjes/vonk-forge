"""Exact accepted capacity promises, reconstructed under consumer admission locks."""

from uuid import UUID, uuid5

from pydantic import BaseModel
from sqlalchemy.orm import Session
from vonk_agent_protocol import ReservationState, UnknownOutcomeError, WaitReason

from .fleet_profile_contract import FleetProfileResourceRequirement
from .models import FleetProfileApplication, ResourceReservation
from .resource_planning import memory_reservation_kind
from .strict_json import read_stored_model


def memory_claim_id(application_id: str, assignment_id: str, node_id: str) -> str:
    return str(
        uuid5(
            UUID(application_id), f"profile-capacity:memory:{assignment_id}:{node_id}"
        )
    )


def capacity_document[T: BaseModel](
    model: type[T], data: str | bytes, *, strict: bool, from_json: bool
) -> T:
    """Damaged stored evidence is re-observed by its accepted child owner."""
    try:
        return read_stored_model(model, data, strict=strict, from_json=from_json)
    except (TypeError, ValueError) as error:
        raise UnknownOutcomeError(
            str(error), reason=WaitReason.SCOPE_CHANGED
        ) from error


def memory_claim(
    session: Session,
    application: FleetProfileApplication,
    assignment_id: str,
    requirement: FleetProfileResourceRequirement,
    *,
    repair: bool,
) -> ResourceReservation | None:
    """Reconstruct only the exact accepted promise under consumer admission locks.

    A handed-off claim remains its consumer's; repair never steals that effect.
    Read-only accounting does not create claims.
    """
    claim = session.get(
        ResourceReservation,
        memory_claim_id(application.id, assignment_id, requirement.node_id),
    )
    if not repair:
        return claim
    if (
        requirement.memory_kind is None
        or requirement.memory_pool is None
        or requirement.memory_required_bytes is None
    ):
        raise UnknownOutcomeError(
            "Accepted memory requirement is unavailable",
            reason=WaitReason.SCOPE_CHANGED,
        )
    if claim is None:
        claim = ResourceReservation(
            id=memory_claim_id(application.id, assignment_id, requirement.node_id),
            node_id=requirement.node_id,
            kind=memory_reservation_kind(requirement.memory_kind),
            resource_key=assignment_id,
            amount_bytes=requirement.memory_required_bytes,
            owner_kind="fleet-profile",
            owner_id=application.id,
            state=ReservationState.PROMISED,
            plan_digest=application.plan_digest,
            created_at=application.created_at,
        )
        session.add(claim)
    elif claim.owner_kind == "fleet-profile" and claim.owner_id == application.id:
        claim.node_id = requirement.node_id
        claim.kind = memory_reservation_kind(requirement.memory_kind)
        claim.resource_key = assignment_id
        claim.amount_bytes = requirement.memory_required_bytes
        claim.state = ReservationState.PROMISED
        claim.plan_digest = application.plan_digest
        claim.released_at = None
    return claim
