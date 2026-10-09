"""Distribution: assignments."""

from __future__ import annotations

from datetime import datetime

from ..distribution_assignment import NodeDistributionAssignment


def _may_replace(
    existing: NodeDistributionAssignment,
    requested: NodeDistributionAssignment,
    *,
    active: bool,
    now: datetime,
) -> bool:
    """Whether a registration may take over a stored (plan, node) grant.

    Equal authorized content can be renewed across request IDs and generations.
    A grant that is no longer live -- revoked, expired, or past its expiry --
    belongs to no transfer and can be reclaimed for different content. A live
    grant for different bound content stays refused.
    """

    def grant(value: NodeDistributionAssignment) -> dict[str, object]:
        mapping = value.to_mapping()
        for field in ("expires_at", "assignment_id", "generation"):
            mapping.pop(field, None)
        return mapping

    return grant(existing) == grant(requested) or not (
        active and existing.expires_at > now
    )
