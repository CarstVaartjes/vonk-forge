from __future__ import annotations


def test_pinned_protocol_wheel_exports_canonical_distribution_types() -> None:
    # This import intentionally exercises the exact wheel selected by
    # control/pyproject.toml, rather than the protocol source checkout.
    from vonk_agent_protocol import DistributionAssignment, DistributionObject

    assert DistributionAssignment.__module__ == "vonk_agent_protocol.distribution"
    assert DistributionObject.__module__ == "vonk_agent_protocol.distribution"
