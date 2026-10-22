"""Display advice is informational and recovers from unknown inventory."""

import json
from pathlib import Path

import pytest
from vonk_agent_protocol import ProjectionCode
from vonk_agent_protocol.agent_words import ProfileReasonSeverity
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.display_memory import (
    DisplayMemoryObservation,
    DisplayMemorySignal,
)
from vonk_agent_protocol.inventory import HostSettings
from vonk_control.fleet_projection.display_memory import display_memory_advice

VECTORS = (
    Path(__file__).parents[2]
    / "agent_protocol/src/vonk_agent_protocol/vectors/display-memory.json"
)


def test_agent_vectors_roundtrip_and_unknown_never_claims_two_gb():
    # Break caught: null evidence becomes an invented default reservation.
    hosts = [
        HostSettings.model_validate_json(canonical_message(value))
        for value in json.loads(VECTORS.read_text())
    ]
    for host in hosts:
        assert HostSettings.model_validate_json(host.model_dump_json()) == host
    assert display_memory_advice(hosts[0]) is not None
    assert display_memory_advice(hosts[1]) is None
    assert display_memory_advice(hosts[2]) is None
    assert display_memory_advice(hosts[3]) is not None


@pytest.mark.parametrize(
    "firmware,update",
    [(0x02009B0B, True), (0x02009E0F, False), (0x02009E10, False), (None, False)],
)
def test_firmware_changes_only_the_advice_not_admission(firmware, update):
    # Break caught: recommending an unnecessary update, or mistaking DMI for SoC.
    host = HostSettings(
        display_manager_active=False,
        graphical_default_target=False,
        display_memory=DisplayMemoryObservation(
            soc_firmware_version=firmware,
            carveout_bytes=2 << 30,
            signal=DisplayMemorySignal.RESERVED_RANGE,
        ),
    )
    advice = display_memory_advice(host)
    assert advice is not None
    assert advice.code == ProjectionCode.HOST_DISPLAY_MEMORY
    assert advice.severity == ProfileReasonSeverity.WARNING.value
    assert advice.recommendation is not None
    assert ("fwupdmgr update" in advice.recommendation) == update
    assert "Advanced → Advanced Menu → Display Reserved Memory" in advice.recommendation
    assert "Display Reserved Memory to 0.5 GB (fixed)" in advice.recommendation
    assert ("GPU requires reset" in advice.recommendation) == update
    assert "1.5 GiB" in advice.detail
    observation = host.display_memory
    assert observation is not None
    for change in (
        {"display_manager_active": True},
        {"graphical_default_target": True},
    ):
        assert display_memory_advice(host.model_copy(update=change)) is None
    assert (
        display_memory_advice(
            host.model_copy(
                update={
                    "display_memory": observation.model_copy(
                        update={"display_connected": True}
                    )
                }
            )
        )
        is not None
    )


@pytest.mark.parametrize("connected", [True, False, None])
@pytest.mark.parametrize("firmware", [0x02009B0B, 0x02009E0F, None])
def test_measured_minimum_needs_no_advice_for_fixed_or_auto(connected, firmware):
    # Break caught: warning a correctly configured fixed 0.5 GB Spark because
    # a monitor is attached or because its firmware is older/unknown.
    host = HostSettings(
        display_manager_active=False,
        graphical_default_target=False,
        display_memory=DisplayMemoryObservation(
            soc_firmware_version=firmware,
            display_connected=connected,
            carveout_bytes=0x20000000,
            signal=DisplayMemorySignal.RESERVED_RANGE,
        ),
    )
    assert display_memory_advice(host) is None
