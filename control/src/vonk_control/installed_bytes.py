"""An installation's recorded size follows what its Spark measures now."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import InstallationBytes, InstallationNodeState

from .models import InstallationNode


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
