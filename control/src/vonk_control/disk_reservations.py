"""Disk commitments not already reflected in authenticated filesystem inventory."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from .inventory_repository import MAX_INVENTORY_FUTURE_SKEW
from .models import InstallationNode, RecipeInstallation, ResourceReservation
from .profile_capacity import reservation_visible
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_installation_plan,
)


def outstanding_disk_reservation_bytes(
    session: Session,
    node_id: str,
    *,
    inventory_observed_at: datetime | None,
    excluded_run_ids: Collection[str] = (),
    excluded_profile_application_ids: Collection[str] = (),
) -> int:
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
            ResourceReservation.state == "active",
            reservation_visible(
                tuple(excluded_profile_application_ids),
                excluded_run_ids=tuple(excluded_run_ids),
            ),
        )
    )
    total = 0
    for reservation, installation, node in rows:
        materialized = 0
        if (
            inventory_observed_at is not None
            and installation is not None
            and installation.state == "installed"
            and node is not None
            and node.state == "installed"
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
                    and plan.image_digest == installation.image_digest
                    and plan.recipe_build_id == installation.recipe_build_id
                ):
                    materialized = owned.required_download_bytes
        total += reservation.amount_bytes - materialized
    return total


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
