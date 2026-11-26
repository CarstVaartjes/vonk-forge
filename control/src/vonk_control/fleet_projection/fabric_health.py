"""Attention items for a fabric port that is down, slow or erroring.

The agent reports each RDMA device's negotiated rate and the link and transport
errors it counted over its last window (``FabricDevice``), and telemetry carries
the number of ACTIVE ports. These are advice: nothing is gated, restarted or
latched, and each item clears with the next clean window or sample.

- Down: the Spark declares a fabric address, its telemetry is not stale, and no
  RDMA port is ACTIVE.
- Rate low: an ACTIVE device negotiated less than the fabric bandwidth the Spark
  declares (a bad cable or transceiver renegotiates 200 Gb/s down to 100 or 40).
- Errors: at least ``FABRIC_ERROR_FLOOR`` link or transport errors in the window.
  Today's evidence is zero on both Sparks (every counter read 0 on 2026-11-25),
  so one error is already a change from a clean baseline; the floor is
  calibrated on hardware.
"""

from __future__ import annotations

from vonk_agent_protocol.agent_words import ProfileReasonSeverity
from vonk_agent_protocol.inventory import HostSettings
from vonk_agent_protocol.reason_codes.profiles import ProjectionCode
from vonk_control.fleet_projection.common import ProjectionReason, TelemetryPoint

FABRIC_ERROR_FLOOR = 1


def fabric_health_warnings(
    host: HostSettings, declared_mbps: int | None
) -> list[ProjectionReason]:
    warnings: list[ProjectionReason] = []
    if declared_mbps is not None:
        slow = [
            device.rate_mbps
            for device in host.fabric_devices
            if device.active
            and device.rate_mbps is not None
            and device.rate_mbps < declared_mbps
        ]
        if slow:
            warnings.append(
                ProjectionReason(
                    code=ProjectionCode.NETWORK_FABRIC_RATE_LOW,
                    detail=(
                        f"A fabric port negotiated {min(slow)} Mb/s; "
                        f"{declared_mbps} Mb/s is declared."
                    ),
                    severity=ProfileReasonSeverity.WARNING.value,
                    recommendation=(
                        "Check the cable and transceiver of the port; a link "
                        "that renegotiated lower stays lower until it is reseated."
                    ),
                )
            )
    erroring = [
        device
        for device in host.fabric_devices
        if device.errors is not None and device.errors >= FABRIC_ERROR_FLOOR
    ]
    if erroring:
        worst = max(erroring, key=lambda device: device.errors or 0)
        warnings.append(
            ProjectionReason(
                code=ProjectionCode.NETWORK_FABRIC_ERRORS,
                detail=(
                    f"A fabric device counted {worst.errors} link or transport "
                    f"errors in {worst.window_seconds} seconds."
                ),
                severity=ProfileReasonSeverity.WARNING.value,
                recommendation=(
                    "Check the cable and transceiver; errors on a clean fabric "
                    "mean a failing link."
                ),
            )
        )
    return warnings


def fabric_down_warning(
    fabric_address: str | None, sample: TelemetryPoint
) -> ProjectionReason | None:
    """``sample`` is the latest of a Spark whose telemetry is not stale."""
    if fabric_address is None or sample.fabric_ports_active != 0:
        return None
    return ProjectionReason(
        code=ProjectionCode.NETWORK_FABRIC_DOWN,
        detail="The Spark declares a fabric address, but no RDMA port is active.",
        severity=ProfileReasonSeverity.WARNING.value,
        recommendation=(
            "Check the fabric cable and the port's link; multi-node runs need "
            "an active port."
        ),
    )
