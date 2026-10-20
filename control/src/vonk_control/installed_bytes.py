"""An installation's recorded size follows what its Spark measures now."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationBytes,
    InstallationNodeState,
    InstallationState,
)
from vonk_agent_protocol.inventory import MAX_REPORTED_INSTALLATIONS

from .models import InstallationNode, RecipeInstallation


def refresh_installed_bytes(
    session: Session, node_id: str, measured: Sequence[InstallationBytes]
) -> int:
    """Record each ended rank's measured size on ``node_id``; how many changed.

    The install receipt measured the tree once; runtime caches grow it after,
    and a failed install's tree was never measured. An installed or failed
    rank follows the measurement, so storage relief counts what removing it
    frees; a rank being installed or removed keeps what its operation records.
    """

    sizes = {item.installation_id: item.bytes for item in measured}
    if not sizes:
        return 0
    changed = 0
    for node in session.scalars(
        select(InstallationNode).where(
            InstallationNode.node_id == node_id,
            InstallationNode.installation_id.in_(tuple(sizes)),
            InstallationNode.state.in_(
                (InstallationNodeState.INSTALLED, InstallationNodeState.FAILED)
            ),
        )
    ):
        if node.installed_bytes != sizes[node.installation_id]:
            node.installed_bytes = sizes[node.installation_id]
            changed += 1
    return changed


def reconcile_missing_installations(
    session: Session,
    node_id: str,
    reported: Sequence[str] | None,
    *,
    observed_at: datetime,
    before: datetime,
) -> int:
    """A complete, fresh listing makes missing installed ranks re-installable.

    Unknown or truncated listings prove nothing. Never overwrite a newer install
    receipt or an in-flight install, and never release running workload claims.
    Busy rows wait for the next inventory rather than blocking its acceptance.
    """
    if reported is None or len(reported) >= MAX_REPORTED_INSTALLATIONS:
        return 0
    candidates = select(InstallationNode.installation_id).where(
        InstallationNode.node_id == node_id,
        InstallationNode.state == InstallationNodeState.INSTALLED,
        InstallationNode.updated_at <= before,
        InstallationNode.installation_id.not_in(tuple(reported)),
    )
    installations = tuple(
        session.scalars(
            select(RecipeInstallation)
            .where(
                RecipeInstallation.id.in_(candidates),
                RecipeInstallation.state.in_(
                    (InstallationState.INSTALLED, InstallationState.PARTIAL)
                ),
                RecipeInstallation.updated_at <= before,
            )
            .order_by(RecipeInstallation.id)
            .with_for_update(skip_locked=True)
        )
    )
    if not installations:
        return 0
    owners = {row.id: row for row in installations}
    changed = 0
    for rank in session.scalars(
        select(InstallationNode)
        .where(
            InstallationNode.installation_id.in_(tuple(owners)),
            InstallationNode.node_id == node_id,
            InstallationNode.state == InstallationNodeState.INSTALLED,
            InstallationNode.updated_at <= before,
        )
        .order_by(InstallationNode.id)
        .with_for_update(skip_locked=True)
    ):
        rank.state = InstallationNodeState.PLANNED
        rank.installed_bytes = 0
        rank.updated_at = observed_at
        installation = owners[rank.installation_id]
        installation.state = InstallationState.PARTIAL
        installation.updated_at = observed_at
        changed += 1
    return changed
