"""Disk commitments not already reflected in authenticated filesystem inventory."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    ReservationState,
)

from .content_identity import same_image
from .inventory_repository import MAX_INVENTORY_FUTURE_SKEW
from .models import InstallationNode, RecipeInstallation, ResourceReservation
from .profile_capacity import reservation_visible
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_installation_plan,
)
from .reservation_owners import reservation_owner


def models_stored_on_node(session: Session, node_id: str) -> frozenset[str]:
    """Models some installation on the Spark holds files of.

    A Spark's shared store receives a model's files before any installation of it
    is made, and only the agent's uninstall of the model's last installation
    removes them, so while such an installation exists the store holds the model
    and a new installation links its files instead of writing them.
    """

    return frozenset(
        digest
        for digest in session.scalars(
            select(RecipeInstallation.model_content_sha256)
            .join(
                InstallationNode,
                InstallationNode.installation_id == RecipeInstallation.id,
            )
            .where(
                InstallationNode.node_id == node_id,
                RecipeInstallation.state.in_(
                    (
                        InstallationState.INSTALLED,
                        InstallationState.INSTALLING,
                        InstallationState.PARTIAL,
                    )
                ),
                RecipeInstallation.model_content_sha256.is_not(None),
            )
        )
        if digest is not None
    )


@dataclass(frozen=True, slots=True)
class DiskCharge:
    """What one owner's claim still adds to a Spark's committed disk."""

    owner_kind: str
    owner_id: str
    amount_bytes: int


def outstanding_disk_reservation_bytes(
    session: Session,
    node_id: str,
    *,
    inventory_observed_at: datetime | None,
    excluded_run_ids: Collection[str] = (),
    excluded_profile_application_ids: Collection[str] = (),
    excluded_installation_ids: Collection[str] = (),
) -> int:
    return sum(
        charge.amount_bytes
        for charge in outstanding_disk_charges(
            session,
            node_id,
            inventory_observed_at=inventory_observed_at,
            excluded_run_ids=excluded_run_ids,
            excluded_profile_application_ids=excluded_profile_application_ids,
            excluded_installation_ids=excluded_installation_ids,
        )
    )


def describe_disk_charges(
    session: Session, charges: Sequence[DiskCharge], *, limit: int = 3
) -> str:
    """Name the owners holding the most committed disk, largest first.

    The blocker shows who holds the bytes (``reserved by cancelled profile
    application 3f68e2f4``), so a leaked claim is visible instead of a bare
    number. Empty when nothing is charged.
    """

    ordered = sorted(
        (charge for charge in charges if charge.amount_bytes > 0),
        key=lambda charge: (-charge.amount_bytes, charge.owner_id),
    )
    parts = [
        f"{reservation_owner(session, charge.owner_kind, charge.owner_id).describe()}"
        f" {charge.amount_bytes} bytes"
        for charge in ordered[:limit]
    ]
    if len(ordered) > limit:
        rest = sum(charge.amount_bytes for charge in ordered[limit:])
        parts.append(f"{len(ordered) - limit} more {rest} bytes")
    return "reserved by " + "; ".join(parts) if parts else ""


def outstanding_disk_charges(
    session: Session,
    node_id: str,
    *,
    inventory_observed_at: datetime | None,
    excluded_run_ids: Collection[str] = (),
    excluded_profile_application_ids: Collection[str] = (),
    excluded_installation_ids: Collection[str] = (),
) -> tuple[DiskCharge, ...]:
    """Keep uncertain commitments; count completed download bytes only once.

    A successful install verifies the exact imported image, archive and compiled
    model projection before acknowledging completion. A later disk observation
    includes those effects. Only the admitted download component can therefore
    be deducted from its reservation once the accepted agent-clock skew cannot
    place the observation before completion; reused bytes were never reserved, and
    staging/cache/rollback headroom remains committed. Logical ``installed_bytes``
    is not a physical allocation counter (projections may be hardlinked).

    This is a read-time accounting projection, not release of the owner's durable
    reservation. Missing, malformed or mismatched ownership keeps the full charge.
    """
    rows = session.execute(
        select(ResourceReservation, RecipeInstallation, InstallationNode)
        .outerjoin(
            RecipeInstallation,
            and_(
                ResourceReservation.owner_kind == "installation",
                ResourceReservation.owner_id == RecipeInstallation.id,
            ),
        )
        .outerjoin(
            InstallationNode,
            and_(
                InstallationNode.installation_id == RecipeInstallation.id,
                InstallationNode.node_id == ResourceReservation.node_id,
            ),
        )
        .where(
            ResourceReservation.node_id == node_id,
            ResourceReservation.kind == "disk",
            ResourceReservation.state == ReservationState.ACTIVE,
            reservation_visible(
                tuple(excluded_profile_application_ids),
                excluded_run_ids=tuple(excluded_run_ids),
                excluded_installation_ids=tuple(excluded_installation_ids),
            ),
        )
    )
    charges: list[DiskCharge] = []
    for reservation, installation, node in rows:
        materialized = 0
        if (
            inventory_observed_at is not None
            and installation is not None
            and installation.state == InstallationState.INSTALLED
            and node is not None
            and node.state == InstallationNodeState.INSTALLED
            and _aware(inventory_observed_at)
            > _aware(node.updated_at) + MAX_INVENTORY_FUTURE_SKEW
            and reservation.plan_digest == installation.plan_digest
        ):
            try:
                plan = parse_stored_installation_plan(installation.plan)
            except RecipeExecutionContractError:
                plan = None
            if plan is not None and plan.plan_digest == reservation.plan_digest:
                owned = next(
                    (item for item in plan.nodes if item.node_id == node_id), None
                )
                if (
                    owned is not None
                    and owned.required_bytes
                    == node.required_bytes
                    == reservation.amount_bytes
                    and owned.required_download_bytes <= owned.required_bytes
                    and owned.rank == node.rank
                    and owned.role == node.role
                    and plan.mapping_id == installation.mapping_id
                    and plan.mapping_generation == installation.mapping_generation
                    and plan.recipe_revision_id == installation.recipe_revision_id
                    and same_image(plan, installation)
                ):
                    materialized = owned.required_download_bytes
        charges.append(
            DiskCharge(
                reservation.owner_kind,
                reservation.owner_id,
                reservation.amount_bytes - materialized,
            )
        )
    return tuple(charges)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
