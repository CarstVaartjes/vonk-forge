"""Fixtures set the runtime images a Spark's newest inventory reports."""

from sqlalchemy import select
from vonk_agent_protocol.runtime_images import RuntimeImageObservation
from vonk_control.models import NodeInventorySnapshot


def report_runtime_images(session, node_id, *image_digests):
    """The Spark's newest inventory lists these images as held by Docker."""
    newest = session.scalars(
        select(NodeInventorySnapshot)
        .where(NodeInventorySnapshot.node_id == node_id)
        .order_by(NodeInventorySnapshot.observed_at.desc())
        .limit(1)
    ).one()
    newest.runtime_images = RuntimeImageObservation(images=list(image_digests))


def clear_runtime_images(session):
    """No Spark reports an image any more."""
    for snapshot in session.scalars(select(NodeInventorySnapshot)):
        snapshot.runtime_images = RuntimeImageObservation(images=[])
