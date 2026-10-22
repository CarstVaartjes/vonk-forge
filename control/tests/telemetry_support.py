"""Build stored samples through the canonical resource owner in fixtures."""

import hashlib
import uuid

from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.telemetry import HostResourceSample
from vonk_control.models import NodeInventoryResourceSnapshot, NodeTelemetrySample


def telemetry_with_resources(
    row: NodeTelemetrySample, resources: HostResourceSample
) -> NodeTelemetrySample:
    if row.id is None:
        row.id = str(uuid.uuid4())
    row.resource_snapshot = NodeInventoryResourceSnapshot(
        node_id=row.node_id,
        observed_at=row.observed_at,
        received_at=row.received_at,
        source_sample_id=row.id,
        host_resources=resources,
        evidence_digest=hashlib.sha256(
            canonical_message(
                {
                    "node_id": row.node_id,
                    "source_sample_id": row.id,
                    "host_resources": resources,
                }
            )
        ).hexdigest(),
    )
    return row
