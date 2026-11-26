"""The attention item for a cabled port that carries traffic on one rail only.

A DGX Spark reaches each 200 Gb/s port through two RDMA devices, and the helper
launches NCCL with both. When NCCL uses only one, the port gives about 112 of
200 Gb/s. The agent reports each device's traffic over the window since its
last report (``FabricDevice``); this compares the two siblings of a port. It is
advice: nothing is restarted, and a window too short to judge, a sibling that is
not ACTIVE (down or without an address, so the helper does not launch it), or no traffic at all give none.

Thresholds, from first principles:

- Busy: at least 8 MiB/s over the window (received plus transmitted). Tensor
  parallel decode moves the activations of every layer for every token, tens of
  MB/s; an idle fabric moves kilobytes per second of keepalives. 8 MiB/s is
  1 GiB per two-minute inventory window, three orders above idle.
- Flat: under 1% of its busy sibling. NCCL splits traffic across the rails it
  uses, so a used rail carries a large share; a rail under 1% is not in use.
- Window: at least 30 s, so a post-stop inventory a few seconds after the last
  report is never judged on a handful of packets.
"""

from __future__ import annotations

from collections import defaultdict

from vonk_agent_protocol.agent_words import ProfileReasonSeverity
from vonk_agent_protocol.host_tuning import FabricDevice
from vonk_agent_protocol.inventory import HostSettings
from vonk_agent_protocol.reason_codes.profiles import ProjectionCode
from vonk_control.fleet_projection.common import ProjectionReason

BUSY_BYTES_PER_SECOND = 8 * 1024 * 1024
FLAT_SHARE_DENOMINATOR = 100
MIN_WINDOW_SECONDS = 30


def is_busy(device: FabricDevice) -> bool:
    return (
        device.window_seconds is not None
        and device.bytes is not None
        and device.window_seconds >= MIN_WINDOW_SECONDS
        and device.bytes >= BUSY_BYTES_PER_SECOND * device.window_seconds
    )


def is_flat_beside(device: FabricDevice, busy: FabricDevice) -> bool:
    return (device.bytes or 0) * FLAT_SHARE_DENOMINATOR < (busy.bytes or 0)


def rails_in_use(devices: list[FabricDevice]) -> tuple[int, int] | None:
    """``(rails used, rails active)`` of the most imbalanced port; None if none."""
    ports: dict[str, list[FabricDevice]] = defaultdict(list)
    for device in devices:
        # A device without a window (its rate only) says nothing of traffic.
        if device.active and device.bytes is not None:
            ports[device.port].append(device)
    worst: tuple[int, int] | None = None
    for siblings in ports.values():
        busiest = max(siblings, key=lambda device: device.bytes or 0)
        if len(siblings) < 2 or not is_busy(busiest):
            continue
        used = sum(not is_flat_beside(device, busiest) for device in siblings)
        if used < len(siblings) and (
            worst is None or used / len(siblings) < worst[0] / worst[1]
        ):
            worst = (used, len(siblings))
    return worst


def fabric_rails_warning(host: HostSettings) -> ProjectionReason | None:
    rails = rails_in_use(host.fabric_devices)
    if rails is None:
        return None
    return ProjectionReason(
        code=ProjectionCode.NETWORK_RAILS_UNDERUSED,
        detail=f"Network using {rails[0]} of {rails[1]} rails.",
        severity=ProfileReasonSeverity.WARNING.value,
        recommendation=(
            "One RDMA device of a cabled port carries no traffic while its "
            "sibling does, so the port gives about half its bandwidth. Check "
            "the multi-node run's NCCL settings; restarting the run repeats "
            "the selection."
        ),
    )
