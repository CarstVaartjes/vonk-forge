from __future__ import annotations

from vonk_agent_protocol.distribution import (
    DistributionAssignment,
    DistributionObject,
)


def test_pinned_protocol_wheel_exports_canonical_distribution_types() -> None:
    # This import intentionally exercises the exact wheel selected by
    # control/pyproject.toml, rather than the protocol source checkout.

    assert DistributionAssignment.__module__ == "vonk_agent_protocol.distribution"
    assert DistributionObject.__module__ == "vonk_agent_protocol.distribution"
