"""Distribution: receipts."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ModelFileState,
)

from ..inventory_items import record_artifact
from ..inventory_items_contract import InventoryArtifactItem, InventoryArtifactKind
from ..models import (
    AgentNode,
    ArtifactDistributionAssignment,
    RecipeBuild,
)


def record_distributed_runtime_image(
    session: Session, *, node_id: str, plan_digest: str, now: datetime
) -> None:
    """Record that a node now holds the runtime image its grant pulled.

    The next plan for that node then sees the image as present and does not
    distribute it again; a 1.7.x update only moves its changed layers anyway.
    """

    assignment = session.scalar(
        select(ArtifactDistributionAssignment).where(
            ArtifactDistributionAssignment.plan_digest == plan_digest,
            ArtifactDistributionAssignment.node_id == node_id,
        )
    )
    if assignment is None:
        return
    build = session.scalar(
        select(RecipeBuild).where(
            RecipeBuild.state == "succeeded",
            RecipeBuild.oci_layout_sha256 == assignment.oci_archive_sha256,
            RecipeBuild.image_digest == assignment.oci_image_digest,
        )
    )
    if build is None or build.image_bytes is None:
        return
    digest = assignment.oci_image_digest.removeprefix("sha256:")
    node = session.get(AgentNode, node_id)
    if node is None:
        return
    record_artifact(
        node,
        InventoryArtifactItem(
            kind=InventoryArtifactKind.IMAGE,
            digest=digest,
            source=f"oci-layout:{assignment.oci_archive_sha256}",
            size_bytes=build.image_bytes,
            state=ModelFileState.VERIFIED,
            ref_count=0,
            verified_at=now,
            updated_at=now,
        ),
    )
