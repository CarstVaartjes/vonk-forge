"""An installation's recorded size follows what its Spark measures now."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentFailureResult,
    HelperErrorCode,
    InstallationBytes,
    InstallationNodeState,
    InstallationState,
)
from vonk_agent_protocol.inventory import MAX_REPORTED_INSTALLATIONS

from .installation_transitions import RankRead, demote_installed_rank
from .machine_states import rank_installed
from .models import InstallationNode, RecipeInstallation, RecipeRun

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
    wait for the next evidence rather than blocking it. Each rank moves by the
    one ``demote_installed_rank`` edge, a compare-and-set on what was read.
    """
    changed = 0
    for rank_id, seen in _demotable(session, node_id, which, before).items():
        if demote_installed_rank(session, seen, before=before, now=observed_at):
            changed += 1
            continue
        # A lost compare-and-set means a newer transition moved the row: read
        # it again once and decide on what it is now, never overwrite it.
        again = _demotable(session, node_id, which, before, rank_id=rank_id)
        if rank_id in again and demote_installed_rank(
            session, again[rank_id], before=before, now=observed_at
        ):
            changed += 1
    return changed


def _demotable(
    session: Session,
    node_id: str,
    which: ColumnElement[bool],
    before: datetime,
    *,
    rank_id: str | None = None,
) -> dict[str, RankRead]:
    """The installed ranks on ``node_id`` that ``which`` selects and that no
    transition newer than ``before`` has touched, as read right now."""

    query = (
        select(
            InstallationNode.id,
            InstallationNode.installation_id,
            InstallationNode.updated_at,
        )
        .join(
            RecipeInstallation,
            RecipeInstallation.id == InstallationNode.installation_id,
        )
        .where(
            InstallationNode.node_id == node_id,
            rank_installed(InstallationNode.state),
            InstallationNode.updated_at <= before,
            RecipeInstallation.state.in_(
                (InstallationState.INSTALLED, InstallationState.PARTIAL)
            ),
            RecipeInstallation.updated_at <= before,
            which,
        )
        .order_by(InstallationNode.id)
    )
    if rank_id is not None:
        query = query.where(InstallationNode.id == rank_id)
    return {
        row.id: RankRead(row.id, row.installation_id, row.updated_at)
        for row in session.execute(query)
    }


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
