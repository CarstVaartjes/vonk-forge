"""One owner for "this Spark reaches the NAS over Wi-Fi" (never gates anything).

Production Sparks that reach the NAS over Wi-Fi share airtime and get a
fraction of the wired throughput. The agent reports interface kind, link speed
and carrier for its NICs plus the interface the NAS route uses; this module
turns that evidence into one typed warning with a recommendation, so the
fleet view, ``vonkctl fleet`` and the web say the same thing. Missing evidence
(an older agent) is unknown and produces no warning.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from vonk_agent_protocol.inventory import NetworkInterface

NasRouteWarningCode = Literal[
    "network.nas-route-wifi-no-wired-port",
    "network.nas-route-wifi-wired-port-down",
    "network.nas-route-wifi-wired-port-unused",
]


@dataclass(frozen=True, slots=True)
class NasRouteNotice:
    code: NasRouteWarningCode
    detail: str
    recommendation: str


def _speed(value: int | None) -> str:
    if value is None:
        return "unknown speed"
    return f"{value / 1000:g} Gb/s" if value >= 1000 else f"{value} Mb/s"


def nas_route_notice(
    interfaces: Sequence[NetworkInterface] | None, route_interface: str | None
) -> NasRouteNotice | None:
    if interfaces is None or route_interface is None:
        return None
    route = next((item for item in interfaces if item.name == route_interface), None)
    if route is None or route.kind != "wifi":
        return None
    detail = (
        f"Reaches the NAS over Wi-Fi ({route.name}, {_speed(route.link_speed_mbps)} "
        "link, shared airtime)."
    )
    wired = [item for item in interfaces if item.kind == "wired"]
    linked = [item for item in wired if item.carrier]
    if linked:
        port = linked[0]
        return NasRouteNotice(
            "network.nas-route-wifi-wired-port-unused",
            detail,
            f"Wired port {port.name} has link ({_speed(port.link_speed_mbps)}) "
            "but is not the NAS route; give it an address on the NAS network "
            "and prefer it over Wi-Fi.",
        )
    if wired:
        return NasRouteNotice(
            "network.nas-route-wifi-wired-port-down",
            detail,
            f"Wired port {wired[0].name} has no link; connect it to the NAS "
            "network with a cable.",
        )
    return NasRouteNotice(
        "network.nas-route-wifi-no-wired-port",
        detail,
        "Connect this Spark to the NAS network over wired Ethernet.",
    )
