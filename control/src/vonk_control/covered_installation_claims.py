"""Release the disk claim of an installation whose files a Spark already reports."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    ReservationState,
)

from .inventory_repository import MAX_INVENTORY_FUTURE_SKEW
from .models import (
    STOPPABLE_RUN_STATES,
    InstallationNode,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from .reservation_owners import ReservationOwner


def release_covered_installation_claims(
    session: Session, now: datetime
) -> tuple[ReservationOwner, ...]:
    """Release the disk claim of an installation whose files the Spark now reports.

    A claim promises bytes the install is still to write. Once the installation
    is installed on a Spark and that Spark has reported its disk after the
    install completed (past the accepted agent-clock skew), the written bytes
    are already subtracted from the reported free space, so the claim counts
    them a second time, and the install's staging space is gone with it. The
    claim is then released, whatever the plan says, until a run of the
    installation is live: a serving workload still writes its caches, and
    those bytes are not in any observation yet. Releasing earlier would let
    admission spend free space an unobserved install is about to use.
    """

    released: list[ReservationOwner] = []
    rows = session.execute(
        select(ResourceReservation, RecipeInstallation, InstallationNode)
        .join(
            RecipeInstallation,
            ResourceReservation.owner_id == RecipeInstallation.id,
        )
        .join(
            InstallationNode,
            (InstallationNode.installation_id == RecipeInstallation.id)
            & (InstallationNode.node_id == ResourceReservation.node_id),
        )
        .where(
            ResourceReservation.owner_kind == "installation",
            ResourceReservation.kind == "disk",
            ResourceReservation.state == ReservationState.ACTIVE,
            RecipeInstallation.state == InstallationState.INSTALLED,
            InstallationNode.state == InstallationNodeState.INSTALLED,
        )
    ).all()
    for reservation, installation, node in rows:
        observed = session.scalar(
            select(func.max(NodeInventorySnapshot.observed_at)).where(
                NodeInventorySnapshot.node_id == reservation.node_id
            )
        )
        if observed is None or _aware(observed) <= (
            _aware(node.updated_at) + MAX_INVENTORY_FUTURE_SKEW
        ):
            continue
        if (
            session.scalar(
                select(RecipeRun.id)
                .where(
                    RecipeRun.installation_id == installation.id,
                    RecipeRun.state.in_(STOPPABLE_RUN_STATES),
                )
                .limit(1)
            )
            is not None
        ):
            continue
        locked = session.scalar(
            select(ResourceReservation)
            .where(
                ResourceReservation.id == reservation.id,
                ResourceReservation.state == ReservationState.ACTIVE,
            )
            .with_for_update(skip_locked=True)
        )
        if locked is None:
            continue
        locked.state = ReservationState.RELEASED
        locked.released_at = now
        released.append(
            ReservationOwner(
                "installation",
                installation.id,
                InstallationState.INSTALLED.value,
                False,
                "installation",
                "files already counted in free space",
            )
        )
    return tuple(released)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
