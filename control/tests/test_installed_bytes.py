"""An installation's recorded size follows what its Spark measures now.

The install receipt measures the tree once; runtime caches grow it after, and a
stale size lets admission and storage relief count space that is not free.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import create_engine, select
from vonk_agent_protocol import InstallationBytes, InstallationNodeState
from vonk_control.installed_bytes import refresh_installed_bytes
from vonk_control.models import Base, InstallationNode

from .test_catalog_revision_collection import NODE, Catalog


def _sizes(world: Catalog) -> dict[str, int]:
    with world.sessions() as session:
        return {
            node.installation_id: node.installed_bytes
            for node in session.scalars(select(InstallationNode))
        }


def test_an_ended_rank_takes_its_measured_size(tmp_path: Path) -> None:
    """Catches a size that never moves after install, a failed install whose
    bytes storage relief never sees, or one that overwrites a rank an
    operation is still deciding."""

    engine = create_engine(f"sqlite:///{tmp_path / 'controller.sqlite'}")
    Base.metadata.create_all(engine)
    world = Catalog(engine)
    revision = world.revision("glm", 1)
    grown, _ = world.workload(revision, run="stopped")
    failed, _ = world.workload(revision, run="stopped")
    planned, _ = world.workload(revision, run="stopped")
    with world.sessions.begin() as session:
        for installation_id, state in (
            (failed, InstallationNodeState.FAILED),
            (planned, InstallationNodeState.PLANNED),
        ):
            rank = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            )
            assert rank is not None
            rank.state = state
    measured = [
        InstallationBytes(installation_id=grown, bytes=7_000),
        InstallationBytes(installation_id=failed, bytes=9_000),
        InstallationBytes(installation_id=planned, bytes=3_000),
        InstallationBytes(installation_id=str(uuid.uuid4()), bytes=5),
    ]

    with world.sessions.begin() as session:
        assert refresh_installed_bytes(session, NODE, measured) == 2
        # Another Spark's measurement never names this Spark's ranks.
        assert refresh_installed_bytes(session, "other-spark", measured) == 0

    assert _sizes(world) == {grown: 7_000, failed: 9_000, planned: 1}
    with world.sessions.begin() as session:
        assert refresh_installed_bytes(session, NODE, measured) == 0
