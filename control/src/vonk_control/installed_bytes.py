"""An installation's recorded size follows what its Spark measures now."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from vonk_agent_protocol.agent_operations import AgentFailureResult
from vonk_agent_protocol.inventory import MAX_REPORTED_INSTALLATIONS, InstallationBytes
from vonk_agent_protocol.reason_codes.helpers import HelperErrorCode
from vonk_agent_protocol.state_machines import InstallationNodeState, InstallationState

from .machine_states import rank_installed
from .models.execution import InstallationNode, RecipeInstallation, RecipeRun

#: The helper proved the runtime image gone from its Spark. An unreadable
#: inspection (``runtime_image_inspect_failed``) proves nothing and never demotes.
_LOST_IMAGE_CODES = frozenset({HelperErrorCode.RUNTIME_IMAGE_MISSING.value})


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

    Unknown or truncated listings prove nothing.
    """
    if reported is None or len(reported) >= MAX_REPORTED_INSTALLATIONS:
        return 0
    return demote_installed_ranks(
        session,
        node_id,
        InstallationNode.installation_id.not_in(tuple(reported)),
        observed_at=observed_at,
        before=before,
    )


def demote_installed_ranks(
    session: Session,
    node_id: str,
    which: ColumnElement[bool],
    *,
    observed_at: datetime,
    before: datetime,
) -> int:
    """The Spark lost what these installed ranks need: install them again.

    ``which`` selects the ranks on ``node_id``. Each becomes planned with no
    bytes and its installation partial, so the next plan installs it afresh
    (and copies whatever the Spark no longer holds). Only rows last changed at
    or before ``before`` move, so a receipt newer than the evidence stays; an
    in-flight install and running workload claims are never touched. Busy rows
    wait for the next evidence rather than blocking it.
    """
    candidates = select(InstallationNode.installation_id).where(
        InstallationNode.node_id == node_id,
        rank_installed(InstallationNode.state),
        InstallationNode.updated_at <= before,
        which,
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
            rank_installed(InstallationNode.state),
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


def demote_ranks_on_lost_image(
    session: Session, result: object, run_id: str | None, node_id: str, now: datetime
) -> None:
    """A Start or a job run of ``run_id`` found its runtime image gone from
    ``node_id`` (removed under an installed rank): every installed rank there
    that names that image is demoted, so the next plan copies the image again
    and installs afresh instead of repeating the same launch."""

    if not (
        isinstance(result, AgentFailureResult)
        and result.helper_error_code in _LOST_IMAGE_CODES
    ):
        return
    run = None if run_id is None else session.get(RecipeRun, run_id)
    installation = (
        None if run is None else session.get(RecipeInstallation, run.installation_id)
    )
    if installation is None:
        return
    demote_installed_ranks(
        session,
        node_id,
        InstallationNode.installation_id.in_(
            select(RecipeInstallation.id).where(
                RecipeInstallation.image_digest == installation.image_digest
            )
        ),
        observed_at=now,
        before=now,
    )
